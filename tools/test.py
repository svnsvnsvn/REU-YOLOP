import argparse
import os, sys
from glob import glob
import pandas as pd
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(BASE_DIR)

import pprint
import torch
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim
import torch.utils.data
import torch.utils.data.distributed
import torchvision.transforms as transforms
import numpy as np
from lib.utils import DataLoaderX

from tensorboardX import SummaryWriter

import lib.dataset as dataset
from lib.config import cfg
from lib.config import update_config
from lib.core.loss import get_loss
from lib.core.function import validate

# Attacks
from lib.core.Attacks.FGSM import run_fgsm_experiments
from lib.core.Attacks.JSMA import run_jsma_experiments

from lib.core.general import fitness
from lib.models import get_net
from lib.utils.utils import create_logger, select_device


from pathlib import Path
import json
import random
import cv2
from tqdm import tqdm
import math
import datetime

from bs4 import BeautifulSoup
import matplotlib.pyplot as plt

def calculate_percentage_drop(initial, current):
    return ((initial - current) / initial) * 100

def flatten_results(df):
    flat_data = []
    for _, row in df.iterrows():
        flat_row = {
            "num_pixels": row["num_pixels"],
            "perturb_value": row["perturb_value"],
            "attack_type": row["attack_type"],
            "loss_avg": row["loss_avg"],
            "time": row["time"][0],  # Assuming you want the first value from the time list
        }
        # Extracting values from tuples/lists
        flat_row["da_segment_iou"] = row["da_segment_result"][0]
        flat_row["da_segment_precision"] = row["da_segment_result"][1]
        flat_row["da_segment_recall"] = row["da_segment_result"][2]
        flat_row["ll_segment_iou"] = row["ll_segment_result"][0]
        flat_row["ll_segment_precision"] = row["ll_segment_result"][1]
        flat_row["ll_segment_recall"] = row["ll_segment_result"][2]
        flat_row["detect_accuracy"] = row["detect_result"][0]
        flat_row["detect_precision"] = row["detect_result"][1]
        flat_row["detect_recall"] = row["detect_result"][2]
        
        flat_data.append(flat_row)
    return pd.DataFrame(flat_data)

def parse_args():
    parser = argparse.ArgumentParser(description='Test Multitask network')

    # philly
    parser.add_argument('--modelDir',
                        help='model directory',
                        type=str,
                        default='')
    parser.add_argument('--logDir',
                        help='log directory',
                        type=str,
                        default='runs/')
    parser.add_argument('--weights', nargs='+', type=str, default='/data2/zwt/wd/YOLOP/runs/BddDataset/detect_and_segbranch_whole/epoch-169.pth', help='model.pth path(s)')
    parser.add_argument('--conf_thres', type=float, default=0.001, help='object confidence threshold')
    parser.add_argument('--iou_thres', type=float, default=0.6, help='IOU threshold for NMS')
   # Adding new arguments for dataset and attack type
    parser.add_argument('--dataset', type=str, choices=['Carla', 'BDD100k'], required=True, help='Choice of dataset: Carla or BDD100k')
    parser.add_argument('--attack', type=str, choices=['FGSM', 'JSMA', 'UAP', 'CCP', 'None'], required=True, help='Choice of attack: FGSM, JSMA, UAP, CCP, or None')
    
    args = parser.parse_args()
    return args

def main():
    # set all the configurations
    args = parse_args()
    update_config(cfg, args)
    
    
    # Attack type selection based on argument
    attack_type = args.attack
    if attack_type == 'None':
        attack_type = None
        print("None selected. Will run only a normal validation.")
    else:
        print(f"{attack_type} selected\n")

    

    # TODO: handle distributed training logger
    # set the logger, tb_log_dir means tensorboard logdir
    logger, final_output_dir, tb_log_dir = create_logger(
        cfg, cfg.LOG_DIR, 'test', attack_type=attack_type)
    
    print(logger)

    logger.info(pprint.pformat(args))
    logger.info(cfg)

    writer_dict = {
        'writer': SummaryWriter(log_dir=tb_log_dir),
        'train_global_steps': 0,
        'valid_global_steps': 0,
    }

    # bulid up model
    # start_time = time.time()
    print("Begin to build up model...\n")
    
    # DP mode
    device = select_device(logger, batch_size=cfg.TEST.BATCH_SIZE_PER_GPU* len(cfg.GPUS)) if not cfg.DEBUG \
        else select_device(logger, 'cpu')
    # device = select_device(logger, 'cpu')

    model = get_net(cfg)
    print("Finish build model\n")
    
    # define loss function (criterion) and optimizer
    criterion = get_loss(cfg, device=device)

    # load checkpoint model
    # det_idx_range = [str(i) for i in range(0,25)]
    model_dict = model.state_dict()
    checkpoint_file = args.weights[0] #args.weights
    logger.info("=> loading checkpoint '{}'".format(checkpoint_file))
    checkpoint = torch.load(checkpoint_file)
    checkpoint_dict = checkpoint['state_dict']
    
    # checkpoint_dict = {k: v for k, v in checkpoint['state_dict'].items() if k.split(".")[1] in det_idx_range}
    model_dict.update(checkpoint_dict)
    model.load_state_dict(model_dict)
    logger.info("=> loaded checkpoint '{}' ".format(checkpoint_file))

    model = model.to(device)
    model.gr = 1.0
    model.nc = 1
    print("Build model finished")

    print("Begin to load data")
    # Data loading
    normalize = transforms.Normalize(
        mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]
    )

    print(f"\n\n\n\ndataset.{cfg.DATASET.DATASET}\n\n\n")
    
    valid_dataset = eval('dataset.' + cfg.DATASET.DATASET)(
        cfg=cfg,
        is_train=False,
        inputsize=cfg.MODEL.IMAGE_SIZE,
        transform=transforms.Compose([
            transforms.ToTensor(),
            normalize,
        ])
    )

    valid_loader = DataLoaderX(
        valid_dataset,
        batch_size=cfg.TEST.BATCH_SIZE_PER_GPU * len(cfg.GPUS),
        shuffle=False,
        num_workers= 0, #cfg.WORKERS # Must be 0 or will cause pickling error
        pin_memory=False,
        collate_fn=dataset.AutoDriveDataset.collate_fn
    )
    
    print('Load data finished')
    
    epoch = 0 #special for test

    startTime = datetime.datetime.now()

    # Normal Validation    
    da_segment_results,ll_segment_results,detect_results, total_loss, maps, times = validate(
    epoch,cfg, valid_loader, valid_dataset, model, criterion,
    final_output_dir, tb_log_dir, writer_dict,
    logger, device
    )
    
    msg = 'Test:    Loss({loss:.3f})\n' \
      'Driving area Segment: Acc({da_seg_acc:.3f})    IOU ({da_seg_iou:.3f})    mIOU({da_seg_miou:.3f})\n' \
      'Lane line Segment: Acc({ll_seg_acc:.3f})    IOU ({ll_seg_iou:.3f})  mIOU({ll_seg_miou:.3f})\n' \
      'Detect: P({p:.3f})  R({r:.3f})  mAP@0.5({map50:.3f})  mAP@0.5:0.95({map:.3f})\n' \
      'Time: inference({t_inf:.4f}s/frame)  nms({t_nms:.4f}s/frame)'.format(
          loss=total_loss, da_seg_acc=da_segment_results[0], da_seg_iou=da_segment_results[1], da_seg_miou=da_segment_results[2],
          ll_seg_acc=ll_segment_results[0], ll_seg_iou=ll_segment_results[1], ll_seg_miou=ll_segment_results[2],
          p=detect_results[0], r=detect_results[1], map50=detect_results[2], map=detect_results[3],
          t_inf=times[0], t_nms=times[1])
      
    logger.info(msg)
    
    normal_metrics = {
    'da_acc_seg': da_segment_results[0],
    'da_IoU_seg': da_segment_results[1],
    'da_mIoU_seg': da_segment_results[2],
    'll_acc_seg': ll_segment_results[0],
    'll_IoU_seg': ll_segment_results[1],
    'll_mIoU_seg': ll_segment_results[2],
    'detect_result': detect_results[2],  # mAP@0.5
    'loss_avg': total_loss,
}
    match attack_type:
        case "FGSM":
            # FGSM
            epsilons = [.03, .05, .1, .15, .2, .3, .5, .75, .9, 1]  # FGSM attack parameters


            results_df = run_fgsm_experiments(model, valid_loader, device, cfg, criterion, epsilons, final_output_dir)
            
            FGSM_percentage_drops = results_df.copy()
            
            results_df['epsilon'] = epsilons

            metrics = ['da_acc_seg', 'da_IoU_seg', 'da_mIoU_seg', 'll_acc_seg', 'll_IoU_seg', 'll_mIoU_seg', 'loss_avg']
            
            for metric in metrics:
                initial_value = normal_metrics[metric]
                percentage_drops[metric] = results_df[metric].apply(lambda x: calculate_percentage_drop(initial_value, x))

            # Create DataFrame for Display
            display_df = pd.DataFrame({'epsilon': epsilons})
            for metric in metrics:
                display_df[metric] = results_df[metric]
                display_df[f'{metric}_drop'] = percentage_drops[metric].apply(lambda x: f'{x:.2f}%')

            # Plotting the DataFrame
            fig, ax = plt.subplots(figsize=(12, 6))
            ax.axis('tight')
            ax.axis('off')

            # Create table
            table = ax.table(cellText=display_df.values, colLabels=display_df.columns, cellLoc='center', loc='center')

            # Save the table as an image
            plt.savefig('FGSM_results.png', bbox_inches='tight', dpi=300)
            plt.show()
            
        case "JSMA":
            # JSMA        
            perturbation_params = [
                (10, 0.1, 'add'),
                (10, 0.1, 'set'),
                (10, 0.1, 'noise'),
                (20, 0.1, 'add'),
                (20, 0.1, 'set'),
                (20, 0.1, 'noise'),
                (30, 0.1, 'add'),
                (30, 0.1, 'set'),
                (30, 0.1, 'noise'),
                (50, 0.1, 'add'),
                (50, 0.1, 'set'),
                (50, 0.1, 'noise'),
                (100, 0.1, 'add'),
                (100, 0.1, 'set'),
                (100, 0.1, 'noise')
            ]

            jsma_results_df = run_jsma_experiments(model, valid_loader, device, cfg, criterion, perturbation_params, final_output_dir)
            
            percentage_drops = jsma_results_df.copy()
            
            for metric in ['da_acc_seg', 'da_IoU_seg', 'da_mIoU_seg', 'll_acc_seg', 'll_IoU_seg', 'll_mIoU_seg', 'loss_avg']:
                initial_value = normal_metrics[metric]
                percentage_drops[metric] = jsma_results_df[metric].apply(lambda x: calculate_percentage_drop(initial_value, x))

            # Function to create and save table for each attack type
            def create_and_save_table(attack_type):
                display_df = jsma_results_df[jsma_results_df['attack_type'] == attack_type].copy()
                display_df['normal_metrics'] = ''

                for metric in ['da_acc_seg', 'da_IoU_seg', 'da_mIoU_seg', 'll_acc_seg', 'll_IoU_seg', 'll_mIoU_seg', 'loss_avg']:
                    display_df[metric] = jsma_results_df[metric]
                    display_df[f'{metric}_drop'] = percentage_drops[metric].apply(lambda x: f'<span style="color:red">{x:.2f}%</span>')

                # Plotting the DataFrame
                fig, ax = plt.subplots(figsize=(12, 6))
                ax.axis('tight')
                ax.axis('off')

                # Create table
                table = ax.table(cellText=display_df.values, colLabels=display_df.columns, cellLoc='center', loc='center')

                # Style the drop columns to be red
                for (i, j), cell in table.get_celld().items():
                    if j > 0 and display_df.columns[j].endswith('_drop'):
                        cell.set_text_props(color='red')

                # Save the table as an image
                plt.savefig(f'JSMA_results_{attack_type}.png', bbox_inches='tight', dpi=300)
                plt.close(fig)
                
            # Create and save tables for each attack type
            for attack_type in jsma_results_df['attack_type'].unique():
                create_and_save_table(attack_type)
    
    print("Test Finish")
    
    print("Starting time: ")
    print(startTime)
    
    endTime = datetime.datetime.now()
    
    print("Ending time: ")
    print(endTime.strftime("%Y-%m-%d %H:%M:%S"))

if __name__ == '__main__':
    main()
    
    

