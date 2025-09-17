from emu24.settings import *
from emu24.helper import *
from glob import glob
import pandas as pd
import numpy as np
import re
from neo.io import BlackrockIO

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

def get_scan_files(patient):
    if patient > "YFJ":
        search_path = f"{PROJECTWORLDS_PATH}/{patient}_Datafile/IMG"
    else:
        search_path = f"{ECOG_PATH}/{patient}Datafile/IMG"
    
    mri_glob = glob(f"{search_path}/*MRI*.nii")
    if len(mri_glob) == 0:
        mri_file = ''
    else:
        mri_file = mri_glob[0]
    ct_glob = glob(f"{search_path}/*CT*.nii")
    if len(ct_glob) == 0:
        ct_file = ''
    else:
        ct_file = ct_glob[0]
    pip_glob = glob(f"{search_path}/{patient}/elec_recon/postInPre.nii.gz")
    if len(pip_glob) == 0:
        pip_file = ''
    else:
        pip_file = pip_glob[0]
    t1_glob = glob(f"{search_path}/{patient}/elec_recon/T1.nii.gz")
    if len(t1_glob) == 0:
        t1_file = ''
    else:
        t1_file = t1_glob[0]
    return mri_file, ct_file, pip_file, t1_file

def scrape_electrode_info(patients):
    # get their electrode csv path
    for patient in patients:
        # get patient and admission for proper unique key
        pt_id = (Patient() & f"emu_id = '{patient}'").fetch1('patient_id')
        admission_id, admission_date = (Admission() & f"patient_id = '{pt_id}'").fetch1('admission_id', 'admission_date')

        # create probe config entry again if it doesn't already exist
        pt_probe_config_id = (ProbeConfig() & f"patient_id = '{pt_id}'").fetch('config_id')
        if pt_probe_config_id.size == 0:
            insert_dict = {}
            # get current max key
            query = ProbeConfig().fetch('config_id')
            if query.size == 0:
                pt_probe_config_id = 0
            else:
                pt_probe_config_id = query.max() + 1
            insert_dict['patient_id'] = pt_id
            insert_dict['admission_id'] = admission_id
            insert_dict['config_id'] = pt_probe_config_id
            insert_dict['start_time'] = admission_date
            insert_dict['end_time'] = ''
            mri_file, ct_file, pip_file, t1_file = get_scan_files(patient)
            insert_dict['mri_file'] = mri_file
            insert_dict['ct_file'] = ct_file
            insert_dict['pip_file'] = pip_file
            insert_dict['t1_file'] = t1_file
            ProbeConfig().insert1(insert_dict)
        else:
            pt_probe_config_id = pt_probe_config_id[0]

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
            insert_dict['config_id'] = pt_probe_config_id
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
                insert_dict['config_id'] = pt_probe_config_id
                # primary keys
                insert_dict['electrode_id'] = row.ElectrodeID
                insert_dict['electrode_label'] = row.Label
                # additional info
                insert_dict['micro_adjacent'] = int(True) if 'micro' in row.Type.lower() else int(False)
                insert_dict['native_x'] = row.Coord_x
                insert_dict['native_y'] = row.Coord_y
                insert_dict['native_z'] = row.Coord_z
                insert_dict['mni305_x'] = row.MNI305_x
                insert_dict['mni305_y'] = row.MNI305_y
                insert_dict['mni305_z'] = row.MNI305_z
                insert_dict['mni152_x'] = None
                insert_dict['mni152_y'] = None
                insert_dict['mni152_z'] = None

                # now insert
                MacroContacts().insert1(insert_dict)

                # now populate base atlas info for this patient
                insert_dict = {
                    'patient_id': pt_id,
                    'admission_id': admission_id,
                    'config_id': pt_probe_config_id,
                    'probe_id': idx,
                    'electrode_id': row.ElectrodeID,
                }
      
                # Get column names dynamically
                roi_col = next((col for col in row.index if col.startswith('ROI_') and col.endswith('mm')), None)
                matter_col = next((col for col in row.index if col.startswith('Matter_') and col.endswith('mm')), None)
                insert_dict['distrio_3m_roi'] = row[roi_col] if roi_col else ""
                insert_dict['xtract_matter'] = row[matter_col] if matter_col else "" 

                # now insert
                BaseAtlasInfo().insert1(insert_dict)               
        # now populate micro contacts for this patient
        # get all micro adjacent macros for this patient
        micro_adjacent_macros =  (MacroContacts() & f"patient_id = '{pt_id}'" & "micro_adjacent = '1'").fetch(as_dict=True)
        # load montage file to get micro labels
        montage_file = f"{DATALAKE_PATH}/{patient}Datafile/INFO/{patient}_montage.xlsx"
        montage_df = pd.read_excel(montage_file, sheet_name='Sheet2')
        # now add all micro by macro
        electrode_df['BaseLabel'] 
        for macro in micro_adjacent_macros:
            # get all micros for this macro
            base_label = re.search(r'^(.*?)(?:\d{2})$', macro['electrode_label']).group(1)
            micro_rows = montage_df[montage_df['ChannelLabel'].str.contains(base_label, regex=False)]

            # also get macro rows to calculate vector trajectory (unit vector)
            macro_coords = (MacroContacts() & f"patient_id = '{pt_id}'" & f"probe_id = '{macro['probe_id']}'" & f"config_id = '{macro['config_id']}'").fetch('native_x', 'native_y', 'native_z')
            unit_vector = get_unit_vector(macro_coords)
            adj_coords = np.array([macro["native_x"], macro["native_y"], macro["native_z"]])
            adj_mni_coords = np.array([macro["mni305_x"], macro["mni305_y"], macro["mni305_z"]])
            micro_coords = adj_coords + unit_vector * 3
            micro_mni_coords = adj_mni_coords + unit_vector * 3
            # now add all micros for this macro
            insert_dict = {
                'patient_id': pt_id,
                'admission_id': admission_id,
                'config_id': pt_probe_config_id,
                'probe_id': macro['probe_id'],
                'native_x': micro_coords[0],
                'native_y': micro_coords[1],
                'native_z': micro_coords[2],
                'mni305_x': micro_mni_coords[0],
                'mni305_y': micro_mni_coords[1],
                'mni305_z': micro_mni_coords[2],
                'mni152_x': None,
                'mni152_y': None,
                'mni152_z': None,
            }
            for idx, row in micro_rows.iterrows():
                insert_dict['electrode_id'] = row.ElectrodeID
                insert_dict['electrode_label'] = row.ChannelLabel
                MicroContacts().insert1(insert_dict)


def get_unit_vector(macro_coords):
    coord_matrix = np.array(macro_coords).T
    # 1️⃣ Compute the centroid of the points
    centroid = np.mean(coord_matrix, axis=0)

    # 2️⃣ Subtract the centroid to center the data
    centered = coord_matrix - centroid

    # 3️⃣ Do Singular Value Decomposition
    _, _, vh = np.linalg.svd(centered)

    # 4️⃣ The first row of vh (or first column of V) is the direction of max variance
    direction = vh[0]

    # 5️⃣ Normalize to get a unit vector
    unit_vector = direction / np.linalg.norm(direction)
    return unit_vector


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
