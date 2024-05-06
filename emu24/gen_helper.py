"""Import * from this script to setup your interpreter for working with the dj schema"""

import argparse
import datajoint as dj

parser = argparse.ArgumentParser()
parser.add_argument('-u', '--username', required=False)
parser.add_argument('-p', '--password', required=False)
args = parser.parse_args()
print('Here')

if args.username and args.password:
    dj.config['database.user'] = args.username
    dj.config['database.password'] = args.password

dj.config['database.host'] = 'localhost'
dj.config['database.port'] = 3306

dj.config['stores'] = {
    "Ext_Chunk": {
        "protocol": "file",
        "location": "/mnt/datalake/data/emu/",
        "stage": "/mnt/datalake/data/emu/"
    },
    "Ext_Stitch": {
        "protocol": "file",
        "location": "/mnt/lake-database/stitched",
        "stage": "/mnt/lake-database/stitched"
    }
}
dj.conn()

print('Importing schema...')
from EMU24 import *

