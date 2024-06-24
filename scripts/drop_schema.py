from emu24.helper import *

if settings.environment != 'prod':

    schema.drop(force=True)