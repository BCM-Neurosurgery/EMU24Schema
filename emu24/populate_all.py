from gen_helper import *

print('Collecting matching NSP data chunks...')
NSPChunks().populate(display_progress=True)

print('Searching for task comments...')
TaskComments().populate(display_progress=True)
StartComments().populate(display_progress=True)
StopComments().populate(display_progress=True)

print('Stitching new data...')
# StitchedChunks().make({'admission_id': 1, 'chunk_id': 1, 'emu_id': 93, 'nsp_id': 1, 'patient_id': 1, 'start_tid': 417, 'stop_tid': 418, 'toc_id': 4})
StitchedChunks().populate(display_progress=True)

