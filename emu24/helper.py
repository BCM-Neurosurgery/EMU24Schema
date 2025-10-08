"""Import * from this script to setup your interpreter for working with the dj schema"""

import argparse
import os

import datajoint as dj
from emu24 import settings

print(f'Using settings for the {settings.environment} environment...')


def make_login_parser():
    login_parser = argparse.ArgumentParser()
    login_parser.add_argument('-u', '--username', required=False)
    login_parser.add_argument('-p', '--password', required=False)
    return login_parser


def connect(cmd_line_args=None, username=None, password=None):
    print('Preparing datajoint connection settings...')
    if cmd_line_args and cmd_line_args.username and cmd_line_args.password:
        dj.config['database.user'] = cmd_line_args.username
        dj.config['database.password'] = cmd_line_args.password
    elif username or password:
        if username:
            print(f'Using the given username')
            dj.config['database.user'] = username
        if password:
            print(f'Using the given password')
            dj.config['database.password'] = password
    else:
        dj.config['database.user'] = os.environ.get('DJ_USER')
        dj.config['database.password'] = os.environ.get('DJ_PASSWORD')


    dj.config['database.host'] = settings.DJ_DATABASE_HOST
    dj.config['database.port'] = settings.DJ_DATABASE_PORT
    dj.config['safemode'] = settings.DJ_CONFIG_SAFEMODE
    dj.config['stores'] = settings.DJ_CONFIG_STORES

    print(f'Connecting...')
    return dj.conn()


if __name__ == "__main__":
    print('Running helper automatic setup')
    args = make_login_parser().parse_args()
    print(args)
    connect(args)
    print('Importing schema...')
    from emu24.schema import *

