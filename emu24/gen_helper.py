"""Import * from this script to setup your interpreter for working with the dj schema"""

import argparse
import datajoint as dj
import settings

print(f'Using settings for the {settings.environment} environment...')

parser = argparse.ArgumentParser()
parser.add_argument('-u', '--username', required=False)
parser.add_argument('-p', '--password', required=False)

args = parser.parse_args()

if args.username and args.password:
    print(f'Using the given username and password...')
    dj.config['database.user'] = args.username
    dj.config['database.password'] = args.password

dj.config['database.host'] = settings.DJ_DATABASE_HOST
dj.config['database.port'] = settings.DJ_DATABASE_PORT
dj.config['safemode'] = settings.DJ_CONFIG_SAFEMODE
dj.config['stores'] = settings.DJ_CONFIG_STORES

print(f'Connecting...')
dj.conn()

print('Importing schema...')
from schema import *


