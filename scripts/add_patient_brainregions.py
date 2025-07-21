from emu24.settings import *
from emu24.helper import *
from glob import glob
import pandas as pd
import numpy as np
import re

# Mapping for broad region codes\ 
BROAD_MAP = {
    'F1': 'superior frontal gyrus',
    'F2': 'middle frontal gyrus',
    'F3': 'inferior frontal gyrus',
    'P1': 'superior parietal lobule',
    'P2': 'inferior parietal lobule',
    'T1': 'superior temporal gyrus',
    'T2': 'middle temporal gyrus',
    'T3': 'inferior temporal gyrus',
    'O1': 'superior occipital gyrus',
    'O2': 'inferior occipital gyrus',
}

# Mapping for specific region prefixes
SPECIFIC_MAP = [
    (r'^(OF|OFC)[a-fA-F]?', 'orbitofrontal cortex'),
    (r'^PH[a-fA-F]?', 'parahippocampal gyrus'),
    (r'^(SMC|SMA)[a-fA-F]?', 'supplementary motor area'),
    (r'^(ANT|AN)[a-fA-F]?', 'anterior nucleus thalamus'),
    (r'^PVN[a-fA-F]?', 'paraventricular nucleus hypothalamus'),
    (r'^CM[a-fA-F]?', 'centromedial nucleus thalamus'),
    (r'^Pulv[a-fA-F]?', 'pulvinar'),
    (r'^E[a-fA-F]?', 'entorhinal cortex'),
    (r'^H[a-fA-F]?', 'hippocampus'),
    (r'^C[a-fA-F]?', 'cingulate cortex'),
    (r'^(I|INS)[a-fA-F]?', 'insula'),
    (r'^A[a-fA-F]?', 'amygdala'),
]

# Regex to split probe name
PATTERN = re.compile(r'^([LR])'               # Side L or R
                     r'([TFPO][1-3][a-f]?)'   # Broad code
                     r'(L[a-f]?)?'           # Optional lesion code
                     r'(.+)?$')               # Optional specific code


def get_patients():
    # grab patients from db
    patient_list = Patient().fetch('emu_id')
    return patient_list

def scrape_electrode_info(patients):
    # get their electrode csv path
    for patient in patients:
        # get patient and admission for proper unique key
        pt_id = (Patient() & f"emu_id = '{patient}'").fetch1('patient_id')
        admission_id = (Admission() & f"patient_id = '{pt_id}'").fetch1('admission_id')
        # check if probe data available for patient already - skip if so
        pt_probes = (Probes() & f"patient_id = '{pt_id}'").fetch('probe_id')
        if pt_probes.size > 0:
            continue
        if patient > "YFJ":
            csv_glob = f"{PROJECTWORLDS_PATH}/{patient}_Datafile/IMG/{patient}*electrodes_v20*.csv"
        else:
            csv_glob = f"{ECOG_PATH}/{patient}Datafile/IMG/{patient}*electrodes_v20*.csv"
        path_match = glob(csv_glob)
        if len(path_match) == 0:
            print("skipping patient", patient, "- no electrode file found")
            continue
        csv_path = path_match[0]
        electrode_df = pd.read_csv(csv_path)
        # get unique probes to insert
        # create base label column

        electrode_df['BaseLabel'] = electrode_df['Label'].str.extract(r'^(.*?)(?:\d{2})$')
        # Get unique base labels
        unique_labels = electrode_df['BaseLabel'].dropna().unique()

        # now we will create entry for each unique label!!!!
        for idx, label in enumerate(unique_labels):
            insert_dict = {}
            insert_dict['patient_id'] = pt_id
            insert_dict['admission_id'] = admission_id 
            insert_dict['probe_id'] = idx
            insert_dict['label'] = label

            # get dataframe subset for this probe
            probe_df = electrode_df[electrode_df['BaseLabel'] == label].reset_index()
            # get number of contacts
            no_contacts = len(probe_df)
            # get whether micro is available
            micros_available = probe_df['Type'].str.contains('micro', case=False, na=False).any()
            # get remaining information
            hemisphere = probe_df.iloc[0].Hemisphere
            manufacturer = probe_df.iloc[0].Manufacturer
            type_ = probe_df.iloc[0].Type
            # put all into dict
            insert_dict["micros_available"] = int(micros_available)
            insert_dict["region_target"] = parse_probe(label)['final_name']
            insert_dict['n_contacts'] = no_contacts
            insert_dict['hemisphere'] = hemisphere
            insert_dict['manufacturer'] = manufacturer
            insert_dict['type'] = type_
            Probes().insert1(insert_dict)

            # now populate electrodes for each probe
            for jdx, row in probe_df.iterrows():
                insert_dict = {}
                # parent keys
                insert_dict['patient_id'] = pt_id
                insert_dict['admission_id'] = admission_id
                insert_dict['probe_id'] = idx
                # primary keys
                insert_dict['electrode_id'] = row.ElectrodeID
                insert_dict['electrode_label'] = row.Label
                # additional info
                insert_dict['micro_adjacent'] = int(True) if 'micro' in row.Type.lower() else int(False)
                insert_dict['coord_x'] = row.Coord_x
                insert_dict['coord_y'] = row.Coord_y
                insert_dict['coord_z'] = row.Coord_z
                insert_dict['mni_x'] = row.MNI305_x
                insert_dict['mni_y'] = row.MNI305_y
                insert_dict['mni_z'] = row.MNI305_z
                insert_dict['scanner_r'] = row.Scanner_R
                insert_dict['scanner_a'] = row.Scanner_A
                insert_dict['scanner_s'] =  row.Scanner_S
                # Get column names dynamically
                roi_col = next((col for col in row.index if col.startswith('ROI_') and col.endswith('mm')), None)
                matter_col = next((col for col in row.index if col.startswith('Matter_') and col.endswith('mm')), None)
                area_col = next((col for col in row.index if col.startswith('Area_fs')), None)
                insert_dict['roi'] = row[roi_col] if roi_col else ""
                insert_dict['matter'] = row[matter_col] if matter_col else ""
                insert_dict['area_fs'] = row[area_col] if area_col else ""
                # now insert!!!
                ElectrodeContacts().insert1(insert_dict)


def parse_probe(label):
    """
    Parse a probe label into its components and construct a descriptive name.
    Returns a dict with keys: broad_raw, broad_name, has_lesion, lesion_raw,
    speicifc_raw, specific_name, final_name.
    """
    m = PATTERN.match(label)
    if not m:
        return {'final_name': 'N/A'}
    
    side, broad_raw, lesion_raw, specific_raw = m.groups()
    # determine broad name
    broad_key = broad_raw[:2]
    broad_name = BROAD_MAP.get(broad_key, None)
    if not broad_name:
        return {'final_name': 'N/A'}
    
    # Lesion flag
    has_lesion = bool(lesion_raw)
    
    # Map specific
    # Parse up to two specific codes at start of specific_raw
    specifics = []
    rest = specific_raw or ''
    while rest and len(specifics) < 2:
        for pattern, label in SPECIFIC_MAP:
            m2 = re.match(pattern, rest)
            if m2:
                code = m2.group(0)
                specifics.append(label)
                rest = rest[len(code):]
                break
        else:
            # no further specific match
            break

    # Build specific_name by concatenating labels
    specific_name = '/'.join(specifics) if specifics else ''
    
    # Build final name
    if has_lesion and not specific_name:
        final = f'{broad_name} lesion'
    elif has_lesion and specific_name:
        final = f'{specific_name} lesion'
    elif not has_lesion and specific_name:
        final = specific_name
    else:
        final = broad_name
    
    return {
        'broad_raw': broad_raw,
        'broad_name': broad_name,
        'has_lesion': has_lesion,
        'lesion_raw': lesion_raw or '',
        'specific_raw': specific_raw or '',
        'specific_name': specific_name or '',
        'final_name': final
    }


   

if __name__ == '__main__':
    # args = login_parser.parse_args()
    connect()
    from emu24.schema import *
    patients = get_patients()
    scrape_electrode_info(patients)
