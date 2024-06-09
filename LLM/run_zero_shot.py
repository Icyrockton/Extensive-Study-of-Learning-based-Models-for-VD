import json
import subprocess
from pathlib import Path

import numpy as np
from config import model_name

all_gpus = [0,1,6,7]
split_cnt = 2


if __name__ == '__main__':
    assert len(all_gpus) % split_cnt == 0 , print('split_cnt should be multiple of gpus')
    per_gpu_num = len(all_gpus) // split_cnt

    all_p = []
    gpus_splits = [x.tolist() for x in np.array_split(all_gpus, split_cnt)]

    for split_idx in range(split_cnt):
        gpus = ','.join([str(gpu) for gpu in gpus_splits[split_idx]])
        p = subprocess.Popen(f'python zero_shot.py --gpus="{gpus}" -split_idx={split_idx} -total_split_cnt={split_cnt}', shell=True,)
        all_p.append(p)

    [x.wait() for x in all_p ]

    # merge result
    print('begin merge result')
    save_dir = Path(f"result/zero_shot/{model_name.replace('/', '-')}")
    for prompt_id in range(100):
        if not (save_dir / f'prompt_{prompt_id}_result_split_0.json').exists():
            break

        print(f'merge prompt{prompt_id}...')
        merge_result = []
        for split_idx in range(split_cnt):
            split_result = json.load((save_dir / f'prompt_{prompt_id}_result_split_{split_idx}.json').open(mode='r'))
            merge_result.extend(split_result)

        json.dump(merge_result, (save_dir / f'prompt_{prompt_id}_result.json').open(mode='w'))