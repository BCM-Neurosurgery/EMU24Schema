from gen_helper import *

if settings.environment != 'prod':

    schema.drop(force=True)