import datajoint as dj
import re
import os

from brpylib import NsxFile
from pyNsXStitch.stitchers import StitchedNeVFile, StitchedNsXFile
from pyNsXStitch.helpers import get_all_nev_comments


def get_emu_id(comment_text):
    """
    Extract the EMU ID number from the comment contents

    All comments linked to a task contain an EMU ID in the form 'EMU-####'
    """
    emu_match = re.search(" EMU-(.*?)", comment_text)
    emu_id = int(emu_match.group(1), 10) if emu_match else 99999
    return emu_id


# Define the schema
schema = dj.schema('paulsteffan_EMU24')

print('Testing updates')


# Define the tables
@schema
class Patient(dj.Manual):
    definition = """
    patient_id: int  # primary key
    ---
    dob: varchar(256) # secondary attribute
    emu_id: varchar(256)
    """


@schema
class Admission(dj.Manual):
    definition = """
    -> Patient
    admission_id: int  # primary key
    ---
    admission_date: varchar(256)  # secondary attribute
    """


@schema
class TOCInstance(dj.Manual):
    definition = """
    -> Admission
    toc_id: int
    ---
    base_file: varchar(256)  # secondary attribute
    """


@schema
class NEVChunks(dj.Manual):
    definition = """
    -> TOCInstance
    chunk_id: int  # primary key
    nsp_id: int
    ---
    nev_file: filepath@Ext_Chunk
    """


@schema
class NS3Chunks(dj.Manual):
    definition = """
    -> TOCInstance
    chunk_id: int  # primary key
    nsp_id: int
    ---
    ns3_file: filepath@Ext_Chunk
    """


@schema
class NS5Chunks(dj.Manual):
    definition = """
    -> TOCInstance
    chunk_id: int
    nsp_id: int
    ---
    ns5_file: filepath@Ext_Chunk
    """


@schema
class NSPChunks(dj.Computed):
    definition = """
    -> NEVChunks
    ---
    file: varchar(256)
    absolute_time: varchar(256)
    nev_file = NULL: filepath@Ext_Chunk
    ns3_file = NULL: filepath@Ext_Chunk
    ns5_file = NULL: filepath@Ext_Chunk
    """
    key_source = NS3Chunks + NS5Chunks

    def make(self, key):
        source = self.key_source

        key_dict = (source & key).fetch1()

        # Get all the primary keys to look up the correct NeV file
        patient, admission = key_dict['patient_id'], key_dict['admission_id']
        toc, nsp, chunk = key_dict['toc_id'], key_dict['nsp_id'], key_dict['chunk_id']
        query = NEVChunks & (
            f"patient_id={patient} "
            f"AND admission_id={admission} "
            f"AND toc_id={toc} "
            f"AND nsp_id={nsp} "
            f"AND chunk_id={chunk}"
        )
        nev_file = query.fetch1('nev_file')

        # Load headers either from ns3 or ns5
        if key_dict['ns3_file'] is not None:
            nsx_fileobj = NsxFile(key_dict['ns3_file'])
            key['file'] = key_dict['ns3_file'][:-4]
        else:
            nsx_fileobj = NsxFile(key_dict['ns5_file'])
            key['file'] = key_dict['ns5_file'][:-4]

        # Extract the absolute time
        header = nsx_fileobj.basic_header
        key['absolute_time'] = str(header['TimeOrigin'])

        key['nev_file'] = nev_file
        if key_dict['ns3_file'] is not None:
            key['ns3_file'] = key_dict['ns3_file']
        if key_dict['ns5_file'] is not None:
            key['ns5_file'] = key_dict['ns5_file']

        # Insert into database
        self.insert1(key)


@schema
class TaskComments(dj.Computed):
    definition = """
    -> NSPChunks
    task_id: int  # primary key
    ---
    comment: varchar(256)  
    timestamp: bigint 
    type: varchar(256)  
    """

    def make(self, key):

        max_id = len(TaskComments())
        # Get the file name

        file = (NSPChunks & key).fetch1('nev_file')
        df = get_all_nev_comments([file])
        if df.empty:
            # Special case foe if there are no comments in this file, so it doesn't get re-computed every time
            max_id += 1
            key['comment'] = "This chunk did not contain any comments"
            key['type'] = 'NOCOMMENT'
            key['timestamp'] = 0
            key['task_id'] = max_id
            self.insert1(key)
            print(f'Saved NOCOMMENTS for {file}')
            return  # No need to continue here
        else:
            print(f'\n Found {len(df)} comments in {file}')
        # Get all comments from the NEV file
        pattern = '$TASK'
        comments = df['Data'].str
        idx = comments.contains(pattern, regex=False)
        matched_entries = df[idx]
        unique_comments = matched_entries.drop_duplicates(subset=matched_entries.columns.difference(['timestamp']))

        for index, row in unique_comments.iterrows():

            if '$TASKID' in row['Data']:
                key['comment'] = row['Data']
                key['type'] = 'TASKID'
            elif '$TASKSTART' in row['Data']:
                key['comment'] = row['Data']
                key['type'] = 'START'
            elif '$TASKSTOP' in row['Data']:
                key['comment'] = row['Data']
                key['type'] = 'STOP'
            elif '$TASKKILL' in row['Data']:
                key['comment'] = row['Data']
                key['type'] = 'KILL'
            elif '$TASKERROR' in row['Data']:
                key['comment'] = row['Data']
                key['type'] = 'ERROR'
            elif '$TASKMETA' in row['Data']:
                key['comment'] = row['Data']
                key['type'] = 'META'
            else:
                # This will throw an error if there are multiple of the same comment that are undefined
                key['comment'] = row['Data']
                key['comment_type'] = 'UNDEFINED'

            max_id += 1
            key['timestamp'] = row['TimeStamps']
            key['task_id'] = max_id

            try:
                self.insert1(key)
            except Exception as e:
                print(key)
                raise e


@schema
class StartComments(dj.Computed):
    definition = """
    -> TaskComments
    emu_id: int
    ---
    start_comment: varchar(256) 
    start_timestamp: bigint
    """
    key_source = TaskComments.proj(
        'type',
        start_comment='comment',
        start_timestamp='timestamp'
    ) & 'type = "START"'

    def make(self, key):

        comment, timestamp = (self.key_source & key).fetch1('start_comment', 'start_timestamp')
        key['start_comment'] = comment
        key['start_timestamp'] = timestamp
        key['emu_id'] = get_emu_id(comment)

        self.insert1(key)


@schema
class TaskIDComments(dj.Computed):
    definition = """
    -> TaskComments
    emu_id: int
    ---
    task_comment: varchar(256)
    task_timestamp: bigint
    task_type: varchar(256)
    """
    key_source = TaskComments.proj(
        'type',
        task_comment='comment',
        task_timestamp='timestamp'
    ) & 'type = "TASKID"'

    def make(self, key):
        comment, timestamp = (self.key_source & key).fetch1('task_comment', 'task_timestamp')
        key['task_comment'] = comment
        key['task_timestamp'] = timestamp
        key['emu_id'] = get_emu_id(comment)

        task_match = re.search("task-(.*)_", comment)
        task_name = task_match.group(1) if task_match else 'UNKNOWN'
        key['task_name'] = task_name


@schema
class StopComments(dj.Computed):
    definition = """
    -> TaskComments
    emu_id: int
    ---
    stop_comment: varchar(256)
    stop_timestamp: bigint
    """
    key_source = TaskComments.proj(
        'type',
        task_comment='task_comment',
        task_timestamp='timestamp'
    ) & ['type = "KILL"', 'type = "STOP"', 'type = "ERROR"']

    def make(self, key):
        comment, timestamp = (self.key_source & key).fetch1('task_comment', 'task_timestamp')
        key['stop_comment'] = comment
        key['stop_timestamp'] = timestamp
        key['emu_id'] = get_emu_id(comment)

        self.insert1(key)


@schema
class StitchedChunks(dj.Computed):
    definition = """
    -> StartComments.proj('start_comment',start_fid='file_id',start_tid='task_id',start_chunk='chunk_id')
    -> StopComments.proj('stop_comment',stop_fid='file_id',stop_tid='task_id',stop_chunk='chunk_id')
    ---
    start_filename: varchar(255)  # secondary attribute
    stop_filename: varchar(255)  # secondary attribute
    nev_file: filepath@Ext_Stitch
    ns3_file = NULL: filepath@Ext_Stitch
    ns5_file = NULL: filepath@Ext_Stitch
    """
    key_source = StartComments.proj(
        'start_comment',
        'start_timestamp',
        start_fid='file_id',
        start_tid='task_id',
        start_chunk='chunk_id'
    ) * StopComments.proj(
        'stop_comment',
        'stop_timestamp',
        stop_fid='file_id',
        stop_tid='task_id',
        stop_chunk='chunk_id'
    )
    identifiers = ['patient_id', 'admission_id', 'toc_id', 'nsp_id', 'chunk_id']
    output = '/mnt/lake-database/stitched'

    def file_lookup(self, key, task_id_col):
        """Lookup the file that a task"""
        task_id = (self.key_source & key).fetch1(task_id_col)
        chunk_keys = (TaskComments & f"task_id={task_id}").fetch1(*self.identifiers)
        chunk_id = chunk_keys[-1]  # Chunk_id is last because of order of identifiers

        nsp_lookup = [f'{name}={value}' for name, value in zip(self.identifiers, chunk_keys)]
        filename = (NSPChunks & ' AND '.join(nsp_lookup)).fetch1('file')
        return filename, chunk_id

    def make(self, key):

        print(key)

        if (self.key_source & key).fetch1('emu_id') == 89:
            pass

        # Get the nev file associated with the start and stop comments
        start_file, start_chunk = self.file_lookup(key, 'start_tid')
        stop_file, stop_chunk = self.file_lookup(key, 'stop_tid')
        key['start_filename'] = start_file
        key['stop_filename'] = stop_file

        # Fetch the start and stop as NSP timestamps
        start_ts = (self.key_source & key).fetch1('start_timestamp')
        end_ts = (self.key_source & key).fetch1('stop_timestamp')

        # Determine the range of missing values and generate the missing files
        all_chunks = list(range(start_chunk, stop_chunk + 1))
        all_files = [f'{start_file[:-3]}{str(num).zfill(3)}' for num in all_chunks]

        # create list of missing entries
        all_nevs = []
        all_nsxs = {'ns3': [], 'ns5': []}
        for entry in all_files:
            nev, ns3, ns5 = (NSPChunks & 'file = "{}"'.format(entry)).fetch1('nev_file', 'ns3_file', 'ns5_file')
            all_nevs.append(nev)
            if ns3 is not None:
                all_nsxs['ns3'].append(ns3)
            if ns5 is not None:
                all_nsxs['ns5'].append(ns5)

        # Fetch any additional metadata needed for file naming
        patient = (Patient & f"patient_id='{key['patient_id']}'").fetch1('emu_id')
        id_comments = (TaskComments & f"timestamp >= {start_ts} AND timestamp < {end_ts} AND comment_type='TASKID' AND nsp_id = {key['nsp_id']}").fetch()
        if not len(id_comments):
            # No suitable task comments found, use a auto-generated name
            task_name = f"EMU-{key['emu_id']}_subj-{patient}_task-UNKNOWN_NSP-{key['nsp_id']}"
        else:
            # Use the first task comment ot generate a name
            task_name = id_comments[0]['task_comment'].split(' ')[-1]

        folder_name = '-'.join(task_name.split('_NSP-')[:-1])   # Drop the NSP id for the folder name
        out_path = os.path.join(self.output, patient, folder_name)
        os.makedirs(out_path, exist_ok=True)

        # Stitch the NEV files
        stitched_nev = StitchedNeVFile(all_nevs, start=start_ts, end=end_ts)
        full_nev_path = os.path.join(out_path, f'{task_name}.nev')
        if os.path.exists(full_nev_path):
            print(f'\nOverwriting old output file: {full_nev_path}')
            os.remove(full_nev_path)
        with open(full_nev_path, 'wb') as f:
            stitched_nev.write(f)
        key['nev_file'] = full_nev_path

        # Stitch and save the locations of the NSX files
        for filetype, files in all_nsxs.items():
            if not files:
                continue  # Skip filetypes that we don't have
            stitched_nsx = StitchedNsXFile(files, start=start_ts, end=end_ts, aggressive_concat=True)
            full_nsx_path = os.path.join(out_path, f'{task_name}.{filetype}')
            if os.path.exists(full_nsx_path):
                print(f'\nOverwriting old output file: {full_nsx_path}')
                os.remove(full_nsx_path)
            with open(full_nsx_path, 'wb+') as f:
                stitched_nsx.write(f)
            key[f'{filetype}_file'] = full_nsx_path

        try:
            self.insert1(key)
        except dj.DataJointError as e:
            print(e)
