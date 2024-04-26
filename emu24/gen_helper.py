"""Import * from this script to setup your interpreter for working with the dj schema"""

import datajoint as dj

dj.config['database.host'] = 'localhost'
dj.config['database.port'] = '3360'

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

from emu24.EMU24 import *

