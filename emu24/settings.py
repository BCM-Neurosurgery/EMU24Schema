import os


DJ_DATABASE_HOST = 'localhost'
DJ_DATABASE_PORT = 3306

# Get the current environment type as a system variable
environment = os.environ.get('ENVIRONMENT', default="development")

if environment == 'dev' or environment == 'development':

    CHECKSUM_SIZE_LIMIT = 1  # Disable checksums for virtually all files

    datalake_path = os.environ.get('DATALAKE_PATH')
    STITCHED_PATH = os.environ.get('STITCHED_PATH')

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
            "location": f"{STITCHED_PATH}",
            "stage": f"{STITCHED_PATH}",
        }
    }

# Run in deploy mode: for initial deployment to the production server but not yet modifying real data
elif environment == 'deploy':

    CHECKSUM_SIZE_LIMIT = 1 * 1024**2  # Disable checksums for files above 1MB

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

    # Checksums for large NsX files dramatically slow down database operations. We use a threshold that
    # disables checksums for virtually all NsX files, but should keep NeV files for data validation
    CHECKSUM_SIZE_LIMIT = 50 * 1024**2  # Disable checksums for files above 50MB

    DATABASE_NAME = 'emu24_stitch'
    STITCHED_PATH = "/mnt/lake-database/stitched"
    DJ_CONFIG_SAFEMODE = True
    DJ_CONFIG_STORES = {
        "Ext_Chunk": {
            "protocol": "file",
            "location": "/mnt/datalake/data/emu/",
            "stage": "/mnt/datalake/data/emu/"
        },
        "Ext_Stitch": {
            "protocol": "file",
            "location": STITCHED_PATH,
            "stage": STITCHED_PATH
        }
    }

