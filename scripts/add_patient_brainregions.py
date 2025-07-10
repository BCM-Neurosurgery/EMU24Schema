from emu24.settings import *
from emu24.helper import *

def get_patients():
    pass
    
def scrape_brain_regions(patients):
    for patient in patients:
        if patient > "YFD":
            ppt_path = f"{DATALAKE_PATH}/{patient}Datafile/INFO/{patient}_electrodes.pptx"
        else:
            ppt_path = f"{ECOG_PATH}/{patient}Datafile/INFO/{patient}_electrodes.pptx"
        continue

if __name__ == '__main__':
    args = login_parser.parse_args()
    connect(args)
    from emu24.schema import *
    patients = get_patients()
    scrape_brain_regions(patients)
