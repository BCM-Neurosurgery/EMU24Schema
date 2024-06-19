"""Import * from this script to setup your interpreter for working with the dj schema"""

import argparse
import datajoint as dj

parser = argparse.ArgumentParser()
parser.add_argument('-u', '--username', required=False)
parser.add_argument('-p', '--password', required=False)
parser.add_argument('-m', '--mode', default="dev")
args = parser.parse_args()
print('Here')

if args.username and args.password:
    dj.config['database.user'] = args.username
    dj.config['database.password'] = args.password

dj.config['database.host'] = 'localhost'
dj.config['database.port'] = 3306

# Run DataJoint in Development mode: for fast local testing and debugging
if args.mode == 'dev':
    dj.config['safemode'] = False
    dj.config['stores'] = {
        "Ext_Chunk": {
            "protocol": "file",
            "location": "data/datalake/data/emu/",
            "stage": "data/datalake/data/emu/"
        },
        "Ext_Stitch": {
            "protocol": "file",
            "location": "/mnt/lake-database/new-stitched",
            "stage": "/mnt/lake-database/new-stitched"
        }
    }

# Run DataJoint in deploy mode: for initial deployment to the production server but not yet modifying real data
elif args.mode == 'deploy':
    dj.config['safemode'] = False
    dj.config['stores'] = {
        "Ext_Chunk": {
            "protocol": "file",
            "location": "/mnt/datalake/test/emu/",
            "stage": "/mnt/datalake/test/emu/"
        },
        "Ext_Stitch": {
            "protocol": "file",
            "location": "/mnt/lake-database/test-stitched",
            "stage": "/mnt/lake-database/test-stitched"
        }
    }

# Run DataJoint in production mode: for true real running conditions
elif args.mode == 'prod':
    dj.config['stores'] = {
        "Ext_Chunk": {
            "protocol": "file",
            "location": "/mnt/datalake/data/emu/",
            "stage": "/mnt/datalake/data/emu/"
        },
        "Ext_Stitch": {
            "protocol": "file",
            "location": "/mnt/lake-database/new-stitched",
            "stage": "/mnt/lake-database/new-stitched"
        }
    }
else:
    raise KeyError("Unknown mode! Must be 'dev', 'prod' or 'deploy'")

dj.conn()

print('Importing schema...')
from emu24.EMU24 import *


