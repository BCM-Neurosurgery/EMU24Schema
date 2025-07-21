import os

DJ_DATABASE_HOST = os.environ.get('DJ_DATABASE_HOST', 'localhost')
DJ_DATABASE_PORT = int(os.environ.get('DJ_DATABASE_PORT', '3306'))

# Get the current environment type as a system variable
environment = os.environ.get('ENVIRONMENT', default="development")

if environment == 'dev' or environment == 'development':
    DATALAKE_PATH = os.environ.get('DATALAKE_PATH')
    STITCHED_PATH = os.environ.get('STITCHED_PATH')
    ECOG_PATH = os.environ.get("ECOG_PATH")
    PROJECTWORLDS_PATH = os.environ.get("PROJECTWORLDS_PATH")
    LOGGING_PATH = os.environ.get('LOGGING_PATH', './log.txt')

    DATABASE_NAME = 'emu24_stitch_dev'
    DJ_CONFIG_SAFEMODE = False

    DJ_CONFIG_STORES = {
        "Ext_Chunk": {
            "protocol": "file",
            "location": f"{DATALAKE_PATH}",
            "stage": f"{DATALAKE_PATH}"
        },
        "Ext_Stitch": {
            "protocol": "file",
            "location": f"{STITCHED_PATH}",
            "stage": f"{STITCHED_PATH}",
        }
    }

# Run in deploy mode: for initial deployment to the production server but not yet modifying real data
elif environment == 'deploy':
    DATABASE_NAME = 'emu24_stitch_deploy'
    STITCHED_PATH = "/mnt/lake-database/test-stitched"
    DJ_CONFIG_SAFEMODE = False
    DJ_CONFIG_STORES = {
        "Ext_Chunk": {
            "protocol": "file",
            "location": "/mnt/datalake/test/emu/",
            "stage": "/mnt/datalake/test/emu/"
        },
        "Ext_Stitch": {
            "protocol": "file",
            "location": STITCHED_PATH,
            "stage": STITCHED_PATH
        }
    }

# Run DataJoint in production mode: for true real running conditions
elif environment == 'prod' or environment == 'production':
    DATABASE_NAME = 'emu24_stitch'
    DATALAKE_PATH = os.environ.get('DATALAKE_PATH', "/mnt/datalake/data/emu/")
    STITCHED_PATH = os.environ.get("STITCHED_PATH", "/mnt/stitched/EMU-18112")
    ECOG_PATH = os.environ.get("ECOG_PATH", "/mnt/datalake/ECoG_backup/ECoG_Data")
    PROJECTWORLDS_PATH = os.environ.get("PROJECTWORLDS_PATH", "/mnt/projectworlds/EMU-18112")
    LOGGING_PATH = os.environ.get("LOGGING_PATH", "/mnt/lake-database/stitched-logs/datajoint_computed_table.log")
    DJ_CONFIG_SAFEMODE = True
    DJ_CONFIG_STORES = {
        "Ext_Chunk": {
            "protocol": "file",
            "location": DATALAKE_PATH,
            "stage": DATALAKE_PATH
        },
        "Ext_Stitch": {
            "protocol": "file",
            "location": STITCHED_PATH,
            "stage": STITCHED_PATH
        }
    }

