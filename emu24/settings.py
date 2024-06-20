import os


DJ_DATABASE_HOST = 'localhost'
DJ_DATABASE_PORT = 3306

# Get the current environment type as a system variable
environment = os.environ.get('ENVIRONMENT', default="development")

if environment == 'dev' or environment == 'development':
    datalake_path = os.environ.get('DATALAKE_PATH')
    stitched_path = os.environ.get('STITCHED_PATH')

    DATABASE_NAME = 'emu24_stitch_dev'
    DJ_CONFIG_SAFEMODE = False

    DJ_CONFIG_STORES = {
        "Ext_Chunk": {
            "protocol": "file",
            "location": f"{datalake_path}",
            "stage": f"{datalake_path}"
        },
        "Ext_Stitch": {
            "protocol": "file",
            "location": f"{stitched_path}",
            "stage": f"{stitched_path}",
        }
    }

# Run in deploy mode: for initial deployment to the production server but not yet modifying real data
elif environment == 'deploy':
    DATABASE_NAME = 'emu24_stitch_deploy'
    DJ_CONFIG_SAFEMODE = False
    DJ_CONFIG_STORES = {
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
elif environment == 'prod' or environment == 'production':
    DATABASE_NAME = 'emu24_stitch'
    DJ_CONFIG_SAFEMODE = True
    DJ_CONFIG_STORES = {
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

