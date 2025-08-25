import datajoint as dj
import re
from typing import Optional
from tqdm import tqdm
from emu24.settings import *
from emu24.helper import connect


def resolve_task(comment_string: str) -> Optional[str]:
    """
    Resolve task name from comment string using waterfall regex matching.
    
    Args:
        comment_string: The comment string to extract task name from
        
    Returns:
        Task name if found, None if no patterns match
    """
    # Define regex patterns in order of priority (most specific to least specific)
    # Each pattern is a tuple: (regex_pattern, custom_task_name, description)
    patterns = [
        (r'ART', 'ART'),
        (r'BDI', 'BDI'),
        (r'Baseline|baseline', 'Baseline'),
        (r'CATDI', 'CATDI'),
        (r'bilingual', 'bilingual'),
        (r'Convo|CONVO|convo|conversation', 'convo'),
        (r'Clinical_stim', 'Clinical_stim'),
        (r'ESSO', 'ESSO'),
        (r'MEM', 'MEM'),
        (r'MST', 'MST'),
        (r'(n|N)etfl?ix', 'netflix'),
        (r'(?i)o(g|r)(-|_| )?pac(k|-)?man', 'og_pacman'),
        (r'(p|P)odcast', 'podcast'),
        (r'sleep', 'sleep'),
        (r'Arithmetic', 'arithmetic'),
        (r'blink_suppression', 'blink_suppression'),
        (r'dialogue', 'dialogue'),
        (r'driving', 'driving'),
        (r'fluency', 'fluency'),
        (r'huthhamilton', 'huthhamilton'),
        (r'jabberwocky', 'jabberwocky'),
        (r'music', 'music'),
        (r'wordlist', 'wordlist'),
        (r'spanish', 'spanish'),
        (r'GoNoGoComplex-jumble', 'GoNoGoComplex-jumble'),
        (r'WheelOfFortune', 'WheelOfFortune'),
        (r'RGB', 'RGB'),
        (r'urge', 'urge'),
        (r'ABCD', 'ABCD'),
        (r'GoNoGoComplex', 'GoNoGoComplex'),
        (r'LOCALIZER', 'LOCALIZER'),
        (r'MentalRotation', 'MentalRotation'),
        (r'FreqPEP', 'FreqPEP'),
        (r'PEP', 'PEP'),
        (r'Pacman', 'Pacman'),
        (r'artnav', 'artnav'),
        (r'noisyAV', 'noisyAV'),
        (r'AD(-|_)?(S|s)weeps', 'AD_sweeps'),
        (r'EyesOpen', 'EyesOpen'),
        (r'GoNoGoSimple', 'GoNoGoSimple'),
        (r'PANAS', 'PANAS'),
        (r'XAI(-|_)stochastic', 'XAI_stochastic'),
        (r'facial-expression', 'facial-expression'),
        (r'visstop', 'visstop'),
        (r'SameDifferentWord', 'SameDifferentWord'),
        (r'Youtube_Music', 'Youtube_Music'),
        (r'IdentifyWordsAV', 'IdentifyWordsAV'),
        (r'Word-Composition', 'Word-Composition'),
        (r'4MAB', '4MAB'),
        (r'anticipation', 'anticipation'),
        (r'troubleshooting', 'troubleshooting'),
        (r'Youtube_MillionDollarBaby', 'Youtube_MillionDollarBaby'),
        (r'socialPac', 'socialPac'),
        (r'Back_Ac', 'Back_Ac'),
        (r'PRT', 'PRT'),
        (r'NFB', 'NFB'),
        (r'PhonemeM', 'PhonemeM'),
        (r'social-faces', 'social-faces'),
        (r'RecDec', 'RecDec'),
        (r'TrustGame', 'TrustGame'),
        (r'Dot-Estimation', 'Dot-Estimation'),
        (r'EyesClosed', 'EyesClosed'),
        (r'Oracle', 'Oracle'),
        (r'TNT', 'TNT'),
        (r'Freqmatchedpulses', 'Freqmatchedpulses'),
        (r'Check_timing', 'Check_timing'),
        (r'Animate_Inanimate', 'Animate_Inanimate'),
        (r'(s|S)tim', 'stim'),
        (r'Trial\d{1,3}_\d{6,9}', 'UNKNOWN'),
        (r'Q\d{1,3}_\d{4}', 'UNKNOWN'),
        (r'EMU-\d{4}_\d{6,10}', 'UNKNOWN'),
        (r'Task', 'Task'),
        (r'EMU-\d{4}_(.*?)(?:_NSP-\d+)?$', 'misc'),     
    ]
    
    # Waterfall matching using for loop
    for pattern, task_name in patterns:
        match = re.search(pattern, comment_string, re.IGNORECASE)
        if match:
            print(f"Matched '{pattern}': returning '{task_name}'")
            if task_name == 'misc':
                print(f"Matched '{pattern}': returning '{match.group(1)}'")
            return task_name if task_name != 'misc' else match.group(1)
    
    # Base case: no patterns matched
    print(f"No patterns matched for comment: '{comment_string}'")
    return 'UNKNOWN'

def coerce_task_names():
    from emu24.schema import TaskIDComments
    """Main function to process and coerce task names from comments."""
    res = TaskIDComments().fetch(as_dict=True)
    
    # Process each comment and resolve task names
    for key in tqdm(res, total=len(res), desc="Processing comments"):
        print(f"\nProcessing comment ID: {key['comment_id']}")
        print(f"Current task name: {key['task_name']}")
        
        # Get the comment string (you'll need to implement this based on your schema)
        # comment_string = get_comment_string(comment_id)  # Placeholder
        
        # rename task from comment
        key['task_name'] = resolve_task(key['comment'])
        print(f"New task name: {key['task_name']}")
        try:
            TaskIDComments().update1(key)
            print(f"Updated task name: {key['task_name']}")
        except Exception as e:
            print(f"Error updating task name: {e}")
            print(f"Key: {key}")

if __name__ == "__main__":
    print("EMU24 Task Name Resolution System")
    connect()
    coerce_task_names()

