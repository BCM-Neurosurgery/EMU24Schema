from gen_helper import *

print('Collecting matching NSP data chunks...')
NSPChunks().populate(display_progress=True)

print('Searching for task comments...')
TaskComments().populate(display_progress=True)
StartComments().populate(display_progress=True)
StopComments().populate(display_progress=True)

print('Stitching new data...')
StitchedChunks().populate(display_progress=True)

