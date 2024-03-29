from __future__ import division

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import numpy as np
import cv2
import math


def bbox_iou(box1, box2):
    b1_x1, b1_y1, b1_x2, b1_y2 = box1[:, 0], box1[:, 1], box1[:, 2], box1[:, 3]
    b2_x1, b2_y1, b2_x2, b2_y2 = box2[:, 0], box2[:, 1], box2[:, 2], box2[:, 3]

    inter_rect_x1 = torch.max(b1_x1, b2_x1)
    inter_rect_y1 = torch.max(b1_y1, b2_y1)
    inter_rect_x2 = torch.min(b1_x2, b2_x2)
    inter_rect_y2 = torch.min(b1_y2, b2_y2)

    inter_area = torch.clamp(inter_rect_x2 - inter_rect_x1 + 1, min=0) *\
        torch.clamp(inter_rect_y2 - inter_rect_y1 + 1, min=0)

    b1_area = (b1_x2 - b1_x1 + 1)*(b1_y2 - b1_y1 + 1)
    b2_area = (b2_x2 - b2_x1 + 1)*(b2_y2 - b2_y1 + 1)

    iou = inter_area / (b1_area + b2_area - inter_area)

    return iou


def predict_transform(prediction, inp_dim, anchors, num_classes, CUDA=True):
    batch_size = prediction.size(0)
    stride = inp_dim // prediction.size(2)
    grid_size = inp_dim // stride
    bbox_attrs = 5 + num_classes
    num_anchors = len(anchors)
    # print(prediction.shape)
    prediction = prediction.clone().view(
        batch_size, bbox_attrs*num_anchors, grid_size*grid_size
    )
    prediction = prediction.clone().transpose(1, 2).contiguous()
    prediction = prediction.clone().view(
        batch_size, grid_size*grid_size*num_anchors, bbox_attrs
    )
    anchors = [(anchor[0]/stride, anchor[1]/stride) for anchor in anchors]
    prediction[:, :, 0] = torch.sigmoid(prediction[:, :, 0].clone())
    prediction[:, :, 1] = torch.sigmoid(prediction[:, :, 1].clone())
    prediction[:, :, 4] = torch.sigmoid(prediction[:, :, 4].clone())
    grid = np.arange(grid_size)
    a, b = np.meshgrid(grid, grid)
    x_offset = torch.FloatTensor(a).view(-1, 1)
    y_offset = torch.FloatTensor(b).view(-1, 1)

    if CUDA:
        x_offset = x_offset.cuda()
        y_offset = y_offset.cuda()
    x_y_offset = torch.cat((x_offset, y_offset), 1).repeat(1, num_anchors).\
        view(-1, 2).unsqueeze(0)
    prediction[:, :, :2] = prediction[:, :, :2].clone() + x_y_offset

    anchors = torch.FloatTensor(anchors)
    if CUDA:
        anchors = anchors.cuda()

    anchors = anchors.repeat(grid_size*grid_size, 1).unsqueeze(0)
    prediction[:, :, 2:4] = torch.clamp(torch.exp(torch.clamp(prediction[:, :, 2:4].clone(), min=-1, max=2))*anchors, min=0.01,max=grid_size)

    #print(prediction[:, :, 2:4])
    prediction[:, :, 5: 5+num_classes] = torch.sigmoid(
        prediction[:, :, 5: 5+num_classes].clone()
    )
    

    return prediction


def unique(tensor):
    tensor_np = tensor.cpu().numpy()
    unique_np = np.unique(tensor_np)
    unique_tensor = torch.from_numpy(unique_np)

    tensor_res = tensor.new(unique_tensor.shape)
    tensor_res.copy_(unique_tensor)

    return tensor_res


def write_results(prediction, confidence, num_classes, nms_conf=0.4):
    conf_max = (prediction[:, :, 4] > confidence).float().unsqueeze(2)
    prediction *= conf_max

    box_corner = prediction.new(prediction.shape)
    box_corner[:, :, 0] = prediction[:, :, 0] - prediction[:, :, 2]/2
    box_corner[:, :, 1] = prediction[:, :, 1] - prediction[:, :, 3]/2
    box_corner[:, :, 2] = prediction[:, :, 0] + prediction[:, :, 2]/2
    box_corner[:, :, 3] = prediction[:, :, 1] + prediction[:, :, 3]/2
    prediction[:, :, :4] = box_corner[:, :, :4]

    batch_size = prediction.size(0)

    write = False

    for ind in range(batch_size):
        image_pred = prediction[ind]
        max_conf, max_conf_idx = torch.max(image_pred[:, 5: 5+num_classes], 1)
        max_conf = max_conf.float().unsqueeze(1)
        max_conf_idx = max_conf_idx.float().unsqueeze(1)
        seq = (image_pred[:, :5], max_conf, max_conf_idx)
        image_pred = torch.cat(seq, 1)
        non_zero_idx = torch.nonzero(image_pred[:, 4])
        try:
            image_pred_ = image_pred[non_zero_idx.squeeze(), :].view(-1, 7)
        except Exception:
            continue

        if image_pred_.shape[0] == 0:
            continue

        image_classes = unique(image_pred_[:, -1])

        for cls in image_classes:
            cls_mask = image_pred_*(image_pred_[:, -1] == cls).float().\
                unsqueeze(1)
            cls_mask_ind = torch.nonzero(cls_mask[:, -2]).squeeze()
            image_pred_class = image_pred_[cls_mask_ind].view(-1, 7)

            conf_sort_index = torch.sort(
                image_pred_class[:, 4], descending=True
            )[1]
            image_pred_class = image_pred_class[conf_sort_index]
            idx = image_pred_class.size(0)
            for i in range(idx):
                try:
                    ious = bbox_iou(
                        image_pred_class[i].unsqueeze(0), image_pred_class[i+1:]
                    )
                except ValueError:
                    break
                except IndexError:
                    break

                iou_mask = (ious < nms_conf).float().unsqueeze(1)
                image_pred_class[i+1:] *= iou_mask

                non_zero_ind = torch.nonzero(image_pred_class[:, 4]).squeeze()
                image_pred_class = image_pred_class[non_zero_ind].view(-1, 7)

            batch_ind = image_pred_class.new(image_pred_class.size(0), 1).\
                fill_(ind)
            seq = batch_ind, image_pred_class

            if not write:
                output = torch.cat(seq, 1)
                write = True
            else:
                out = torch.cat(seq, 1)
                output = torch.cat((output, out))

    try:
        return output
    except Exception:
        return 0


def load_classes(namesfile):
    fb = open(namesfile, 'r')
    names = fb.read().split("\n")[:-1]
    return names


def letterbox_image(img, inp_dim):
    '''resize image with unchanged aspect ratio using padding'''
    img_w, img_h = img.shape[1], img.shape[0]
    w, h = inp_dim
    new_w = int(img_w * min(w/img_w, h/img_h))
    new_h = int(img_h * min(w/img_w, h/img_h))
    resized_image = cv2.resize(
        img, (new_w, new_h), interpolation=cv2.INTER_CUBIC
    )

    canvas = np.full((inp_dim[1], inp_dim[0], 3), 128)

    canvas[
        (h-new_h)//2:(h-new_h)//2 + new_h, (w-new_w)//2:(w-new_w)//2 + new_w, :
    ] = resized_image

    return canvas


def prep_image(img, inp_dim):
    """
    Prepare image for inputting to the neural network.
    Returns a Variable
    """
    img = (letterbox_image(img, (inp_dim, inp_dim)))
    img = img[:, :, ::-1].transpose((2, 0, 1)).copy()
    img = torch.from_numpy(img).float().div(255.0).unsqueeze(0)
    return img






# test_tensor = torch.load('/home/saman/python-projects/yolo-object-detection/test_tensor.pt')
# conf_max = (test_tensor[:, :, 4] > 0.5).float().unsqueeze(2)
# print(test_tensor*conf_max)
# print(test_tensor[0, 1, 5])
# bef_test_tensor = torch.load('/home/saman/python-projects/yolo-object-detection/bef_test_tensor.pt')
# print(bef_test_tensor[0, 1, 5, 5])
# box_corner = test_tensor.new(test_tensor.shape)
# print(box_corner)
# img = test_tensor[0]
# idx, score = torch.max(img[:, 5: 85], 1)
# print(score.float().unsqueeze(1))
# print(len(img[:, -1]))
# print(len(np.unique(img[:, -1])))
# print(test_tensor.size())
# new_tensor = test_tensor*((test_tensor[:, :, 4] > 0.5).float().unsqueeze(2))
# image_pred = new_tensor[0]
# max_conf, max_conf_idx = torch.max(image_pred[:, 5: 85], 1)
# max_conf = max_conf.float().unsqueeze(1)
# max_conf_idx = max_conf_idx.float().unsqueeze(1)
# seq = (image_pred[:, :5], max_conf, max_conf_idx)
# image_pred = torch.cat(seq, 1)
# non_zero_idx = torch.nonzero(image_pred[:, 4])
# image_pred_ = image_pred[non_zero_idx.squeeze(), :].view(-1, 7)
# image_classes = unique(image_pred_[:, -1])
# grid = np.arange(19)
# a, b = np.meshgrid(grid, grid)
# print(torch.FloatTensor(b).view(-1, 1))
