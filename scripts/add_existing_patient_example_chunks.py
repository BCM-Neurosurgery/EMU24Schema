from emu24.settings import *
from emu24.helper import *
from glob import glob
import pandas as pd
import numpy as np
import re
import random
import re
from tqdm import tqdm

def get_patients():
    # grab patients from db
    patient_list = Patient().fetch('emu_id')
    return patient_list

def add_patient_chunks(patients):
    # get their electrode csv path
    # define match string
    PATTERN = re.compile(r"^NSP([1-2])-\d{8}-\d{6}-(\d{3}).n.{2}$")
    for patient in patients:
        print("starting patient:", patient)
        # get patient and admission for proper unique key
        pt_id = (Patient() & f"emu_id = '{patient}'").fetch1('patient_id')
        admission_id = (Admission() & f"patient_id = '{pt_id}'").fetch1('admission_id')
        # get all toc recording dirs for each patient
        toc_dirs = glob(f"{DATALAKE_PATH}/{patient}Datafile/DATA/*")
        if not toc_dirs:
            return
        # randomly sample 5
        elif len(toc_dirs) > 5:
            toc_dirs = random.sample(toc_dirs, 5)
        # loop through toc dirs and grab recording files
        for idx, toc_dir in enumerate(toc_dirs):
            toc_insert_dict = {
                'patient_id': pt_id,
                'admission_id': admission_id,
                'toc_id': idx,
                'base_file': os.path.basename(toc_dir),
            }
            # insert recording
            TOCInstance().insert1(toc_insert_dict, skip_duplicates=True)
            
            # now get and insert chunks
            ns5_chunks = glob(f"{toc_dir}/*.ns5")
            nev_chunks = glob(f"{toc_dir}/*.nev")
            ns3_chunks = glob(f"{toc_dir}/*.ns3")
            print("starting ns5 chunks")
            for chunk in tqdm(ns5_chunks):
                chunk_insert_dict = {
                    'patient_id': pt_id,
                    'admission_id': admission_id,
                    'toc_id': idx,
                }
                basename = os.path.basename(chunk)
                match = PATTERN.match(basename)
                if match:
                    nsp_id, chunk_id = match.groups()
                else:
                    nsp_id, chunk_id = -1
                chunk_insert_dict['nsp_id'] = int(nsp_id)
                chunk_insert_dict['chunk_id'] = int(chunk_id)
                chunk_insert_dict['ns5_file'] = chunk
                NS5Chunks().insert1(chunk_insert_dict, skip_duplicates=True)
            print("starting nev chunks")
            for chunk in tqdm(nev_chunks):
                chunk_insert_dict = {
                    'patient_id': pt_id,
                    'admission_id': admission_id,
                    'toc_id': idx,
                }
                basename = os.path.basename(chunk)
                match = PATTERN.match(basename)
                if match:
                    nsp_id, chunk_id = match.groups()
                else:
                    nsp_id, chunk_id = -1
                chunk_insert_dict['nsp_id'] = int(nsp_id)
                chunk_insert_dict['chunk_id'] = int(chunk_id)
                chunk_insert_dict['nev_file'] = chunk
                NEVChunks().insert1(chunk_insert_dict, skip_duplicates=True)
            print("starting ns3 chunks")
            for chunk in tqdm(ns3_chunks):
                chunk_insert_dict = {
                    'patient_id': pt_id,
                    'admission_id': admission_id,
                    'toc_id': idx,
                }
                basename = os.path.basename(chunk)
                match = PATTERN.match(basename)
                if match:
                    nsp_id, chunk_id = match.groups()
                else:
                    nsp_id, chunk_id = -1
                chunk_insert_dict['nsp_id'] = int(nsp_id)
                chunk_insert_dict['chunk_id'] = int(chunk_id)
                chunk_insert_dict['ns3_file'] = chunk
                NS3Chunks().insert1(chunk_insert_dict, skip_duplicates=True)
   

if __name__ == '__main__':
    # args = login_parser.parse_args()
    connect()
    from emu24.schema import *
    patients = get_patients()
    add_patient_chunks(patients)
