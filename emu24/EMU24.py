import datajoint as dj
import re
import os

from brpylib import NsxFile
from pyNsXStitch.stitchers import StitchedNeVFile, StitchedNsXFile
from pyNsXStitch.helpers import get_all_nev_comments


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
    -> NS5Chunks
    -> NS3Chunks
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
            f"patient_id='{patient}'"
            f"& admission_id='{admission}'"
            f"& toc_id='{toc}'"
            f"& nsp_id='{nsp}'"
            f"& chunk_id='{chunk}'"
        )
        nev_file = query.fetch1('nev_file')

        # Load headers either from ns3 or ns5
        try:
            nsx_fileobj = NsxFile(key_dict['ns3_file'])
            key['file'] = key_dict['ns3_file'][:-4]
        except KeyError:
            nsx_fileobj = NsxFile(key_dict['ns5_file'])
            key['file'] = key_dict['ns5_file'][:-4]

        # Extract the absolute time
        header = nsx_fileobj.basic_header
        key['absolute_time'] = str(header['TimeOrigin'])

        key['nev_file'] = nev_file
        if 'ns3_file' in key_dict:
            key['ns3_file'] = key_dict['ns3_file']
        if 'ns5_file' in key_dict:
            key['ns5_file'] = key_dict['ns5_file']

        # Insert into database
        self.insert1(key)


@schema
class TaskComments(dj.Computed):
    definition = """
    -> NSPChunks
    task_id: int  # primary key
    ---
    task_comment: varchar(256)  
    timestamp: int 
    comment_type: varchar(256)  
    """

    def make(self, key):

        max_id = len(TaskComments())
        # Get the file name

        file = (NSPChunks & key).fetch1('nev_file')
        df = get_all_nev_comments([file])
        # Get all comments from the NEV file
        pattern = '$TASK'
        comments = df['Data'].str
        idx = comments.contains(pattern, regex=False)
        matched_entries = df[idx]
        unique_comments = matched_entries.drop_duplicates()
        for index, row in unique_comments.iterrows():
            if '$TASKID' in row['Data']:
                key['task_comment'] = row['Data']
                key['comment_type'] = 'TASKID'
            elif '$TASKSTART' in row['Data']:
                key['task_comment'] = row['Data']
                key['comment_type'] = 'START'
            elif '$TASKSTOP' in row['Data']:
                key['task_comment'] = row['Data']
                key['comment_type'] = 'STOP'
            elif '$TASKKILL' in row['Data']:
                key['task_comment'] = row['Data']
                key['comment_type'] = 'KILL'
            elif '$TASKERROR' in row['Data']:
                key['task_comment'] = row['Data']
                key['comment_type'] = 'ERROR'
            elif '$TASKMETA' in row['Data']:
                key['task_comment'] = row['Data']
                key['comment_type'] = 'META'
            else:
                # This will throw an error if there are multiple of the same comment that are undefined
                key['task_comment'] = row['Data']
                key['comment_type'] = 'UNDEFINED'

            max_id = max_id + 1
            key['timestamp'] = row['TimeStamps']
            key['task_id'] = max_id
            self.insert1(key)


@schema
class StartComments(dj.Computed):
    definition = """
    -> TaskComments
    emu_id: int
    ---
    start_comment: varchar(256) 
    start_timestamp: int
    task_name: varchar(255)  # secondary attribute
    """
    key_source = TaskComments.proj(
        'comment_type',
        start_comment='task_comment',
        start_timestamp='timestamp'
    ) & 'comment_type = "TASKID"'

    def make(self, key):

        comment, timestamp = (TaskComments.proj('comment_type',start_comment='task_comment',start_timestamp='timestamp')  &  key).fetch1('start_comment', 'start_timestamp')
        key['start_comment'] = comment
        key['start_timestamp'] = timestamp
        task_pattern = "task-(.*?)_"
        emu_pattern = " EMU-(.*?)_subj"

        # Using re.search() to find the pattern in the string
        task_match = re.search(task_pattern, comment)
        emu_match = re.search(emu_pattern, comment)

        # Extracting the matched group, which is the part of the string we want
        if task_match:
            key["task_name"] = task_match.group(1)
        else:
            key["task_name"] = "UNDEFINED"

        if emu_match:
            key["emu_id"] = int(emu_match.group(1), 10)
        else:
            key["emu_id"] = 99999

        self.insert1(key)


@schema
class StopComments(dj.Computed):
    definition = """
    -> TaskComments
    emu_id: int
    ---
    stop_comment: varchar(256)
    stop_timestamp: int
    """
    key_source = TaskComments.proj(
        'comment_type',
        stop_comment='task_comment',
        stop_timestamp='timestamp'
    ) & ['comment_type = "KILL"', 'comment_type = "STOP"']  # TODO: Add Error comment as a termination

    def make(self, key):
        comment, timestamp = (TaskComments.proj(
            'comment_type',
            stop_comment='task_comment',
            stop_timestamp='timestamp'
        ) & key).fetch1('stop_comment', 'stop_timestamp')
        key['stop_comment'] = comment
        key['stop_timestamp'] = timestamp
        emu_pattern = " EMU-(.*)"

        # Using re.search() to find the pattern in the string
        emu_match = re.search(emu_pattern, comment)

        # Extracting the matched group, which is the part of the string we want
        if emu_match:
            key["emu_id"] = int(emu_match.group(1), 10)
        else:
            key["emu_id"] = 99999
        self.insert1(key)


@schema
class StitchedChunks(dj.Computed):
    definition = """
    -> StartComments.proj('start_comment',start_fid='file_id',start_tid='task_id')
    -> StopComments.proj('stop_comment',stop_fid='file_id',stop_tid='task_id')
    ---
    start_filename: varchar(255)  # secondary attribute
    stop_filename: varchar(255)  # secondary attribute
    nev_file: filepath@Ext_Stitch
    ns3_file: filepath@Ext_Stitch
    ns5_file: filepath@Ext_Stitch
    """
    key_source = StartComments.proj(
        'start_comment',
        'start_timestamp',
        start_fid='file_id',
        start_tid='task_id'
    ) * StopComments.proj(
        'stop_comment',
        'stop_timestamp',
        stop_fid='file_id',
        stop_tid='task_id'
    )

    def make(self, key):
        # Get the nev file associated with the start and stop comments
        source = StartComments.proj(
            'start_comment',
            'start_timestamp',
            start_fid='file_id',
            start_tid='task_id'
        ) * StopComments.proj(
            'stop_comment',
            'stop_timestamp',
            stop_fid='file_id',
            stop_tid='task_id'
        )

        start_file = (NSPChunks & ('file_id = ' + str((source & key).fetch1('start_fid')))).fetch1('file')
        stop_file = (NSPChunks & ('file_id = '+str((source & key).fetch1('stop_fid')))).fetch1('file')

        # Extract last three digits from the strings
        last_digits_start = int(start_file.split('-')[-1])
        last_digits_stop = int(stop_file.split('-')[-1])

        # Determine the range of missing values
        missing_range = range(last_digits_start + 1, last_digits_stop)

        # Generate the missing entries
        missing_entries = [f'{start_file[:-3]}{str(num).zfill(3)}' for num in missing_range]

        entries = missing_entries
        entries.insert(0, start_file)
        entries.append(stop_file)

        # create list of missing entries
        nev = []
        ns3 = []
        ns5 = []
        for entry in entries:
            nev, ns3, ns5 = (NSPChunks & 'file = "{}"'.format(entry)).fetch1('nev_file', 'ns3_file', 'ns5_file')
            nev.append(nev)
            ns3.append(ns3)
            ns5.append(ns5)

        emu_id = (source & key).fetch1('emu_id')

        # TODO: Lump these into a loop over available filetypes
        # Stitch the NEV files
        stitched_nev = StitchedNeVFile(
            nev,
            start=(source & key).fetch1('start_timestamp'),
            end=(source & key).fetch1('stop_timestamp')
        )
        full_nev_path = os.path.join('/app/Data/EMU24/Ext_Stitch', f'EMU{emu_id}-stitched.nev')
        with open(full_nev_path, 'wb') as f:
            stitched_nev.write(f)

        # Stitch the NS3 files
        stitched_ns3 = StitchedNsXFile(
            ns3,
            start=(source & key).fetch1('start_timestamp'),
            end=(source & key).fetch1('stop_timestamp')
        )
        full_ns3_path = os.path.join('/app/Data/EMU24/Ext_Stitch', f'EMU{emu_id}-stitched.ns3')
        with open(full_ns3_path, 'wb') as f:
            stitched_ns3.write(f)

        # Stitch the NS5 files
        stitched_ns5 = StitchedNsXFile(
            ns5,
            start=(source & key).fetch1('start_timestamp'),
            end=(source & key).fetch1('stop_timestamp')
        )
        full_ns5_path = os.path.join('/app/Data/EMU24/Ext_Stitch', f'EMU{emu_id}-stitched.ns5')
        with open(full_ns5_path, 'wb') as f:
            stitched_ns5.write(f)

        key['nev_file'] = full_nev_path
        key['ns3_file'] = full_ns3_path
        key['ns5_file'] = full_ns5_path
        key['start_filename'] = start_file
        key['stop_filename'] = stop_file
        self.insert1(key)
