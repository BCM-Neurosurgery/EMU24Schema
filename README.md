# EMU 24/7 Stitching Schema

This is a datajoint schema that is responsible for keeping track of all
NSP recordings done in the EMU, and stitching together TOC mode files into
Task-mode equivalent NSP files.


## Contributing

To pull the code, simply clone down the github repo. As always, please commit your changes
on a dedicated branch and then open a pull request to the main or development branch.

To run the code locally, you will need to set up a datajoint-compatible SQL 
database on your local development machine. Make sure that the database is 
available at localhost on port 3306.

The schema will automatically run in development mode, unless you change the 
`ENVIRONMENT` environment variable in on your computer. This is the recommended 
mode for development purposes.

You will also need to set the `DATALAKE_PATH` and `STITCHED_PATH` environment variables.
These point to the base path of the datalake and the output directory for the stitched data
respectively.

All the scripts (in `scripts/`) are set up to be runnable and to automatically connect to your local database. You can
pass your username and password to these scripts as command line arguments to avoid repeatedly typing these out.

If you are using the schema in interactive mode, then you will need to manually connect to the database.
```pyhton
from emu24.helper import *
connect()
from emu24.schema import * 
```



