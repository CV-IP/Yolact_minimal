import torch
import cv2
import numpy as np
from numpy import random
import torch.nn.functional as F
from data.config import cfg, MEANS, STD


def intersect(box_a, box_b):
    max_xy = np.minimum(box_a[:, 2:], box_b[2:])
    min_xy = np.maximum(box_a[:, :2], box_b[:2])
    inter = np.clip((max_xy - min_xy), a_min=0, a_max=np.inf)
    return inter[:, 0] * inter[:, 1]


def jaccard_numpy(box_a, box_b):
    inter = intersect(box_a, box_b)
    area_a = ((box_a[:, 2] - box_a[:, 0]) *
              (box_a[:, 3] - box_a[:, 1]))  # [A,B]
    area_b = ((box_b[2] - box_b[0]) *
              (box_b[3] - box_b[1]))  # [A,B]
    union = area_a + area_b - inter

    return inter / union  # [A,B]


class Compose(object):
    def __init__(self, transforms):
        self.transforms = transforms

    def __call__(self, img, masks=None, boxes=None, labels=None):
        for one_transform in self.transforms:
            img, masks, boxes, labels = one_transform(img, masks, boxes, labels)

        return img, masks, boxes, labels


class ImgToFloat(object):
    def __call__(self, image, masks=None, boxes=None, labels=None):
        return image.astype(np.float32), masks, boxes, labels


class ToAbsoluteBox(object):
    def __call__(self, image, masks=None, boxes=None, labels=None):
        height, width, _ = image.shape
        boxes[:, 0] *= width
        boxes[:, 2] *= width
        boxes[:, 1] *= height
        boxes[:, 3] *= height

        return image, masks, boxes, labels


class ToPercentBox(object):
    def __call__(self, image, masks=None, boxes=None, labels=None):
        height, width, channels = image.shape
        boxes[:, 0] /= width
        boxes[:, 2] /= width
        boxes[:, 1] /= height
        boxes[:, 3] /= height

        return image, masks, boxes, labels


class Resize(object):
    def __init__(self, resize_gt=True):
        self.resize_gt = resize_gt

    def __call__(self, image, masks, boxes, labels=None):
        original_h, original_w, _ = image.shape
        image = cv2.resize(image, (cfg.img_size, cfg.img_size))

        if self.resize_gt:
            masks = masks.transpose((1, 2, 0))
            masks = cv2.resize(masks, (cfg.img_size, cfg.img_size))

            if len(masks.shape) == 2:  # OpenCV resizes a (w,h,1) array to (s,s), so fix that
                masks = np.expand_dims(masks, 0)
            else:
                masks = masks.transpose((2, 0, 1))

            # Scale bounding boxes (which are currently absolute coordinates)
            boxes[:, [0, 2]] *= (cfg.img_size / original_w)
            boxes[:, [1, 3]] *= (cfg.img_size / original_h)

        return image, masks, boxes, labels


class RandomContrast(object):
    def __init__(self, lower=0.7, upper=1.3):
        self.lower = lower
        self.upper = upper
        assert self.upper >= self.lower, "contrast upper must be >= lower."
        assert self.lower >= 0, "contrast lower must be non-negative."

    def __call__(self, image, masks=None, boxes=None, labels=None):
        alpha = random.uniform(self.lower, self.upper)
        image *= alpha
        return image, masks, boxes, labels


class RandomBrightness(object):
    def __init__(self, delta=20.0):  # delta must between 0 ~ 255
        self.delta = delta

    def __call__(self, image, masks=None, boxes=None, labels=None):
        delta = random.uniform(-self.delta, self.delta)
        image += delta
        return image, masks, boxes, labels


class ConvertColor(object):
    def __init__(self, current='BGR', transform='HSV'):
        self.transform = transform
        self.current = current

    def __call__(self, image, masks=None, boxes=None, labels=None):
        if self.current == 'BGR' and self.transform == 'HSV':
            image = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        elif self.current == 'HSV' and self.transform == 'BGR':
            image = cv2.cvtColor(image, cv2.COLOR_HSV2BGR)
        else:
            raise NotImplementedError
        return image, masks, boxes, labels


class RandomSaturation(object):
    def __init__(self, lower=0.7, upper=1.3):
        self.lower = lower
        self.upper = upper
        assert self.upper >= self.lower, "contrast upper must be >= lower."
        assert self.lower >= 0, "contrast lower must be non-negative."

    def __call__(self, image, masks=None, boxes=None, labels=None):
        image[:, :, 1] *= random.uniform(self.lower, self.upper)
        return image, masks, boxes, labels


class RandomHue(object):
    def __init__(self, delta=12.0):
        assert 0.0 <= delta <= 360.0
        self.delta = delta

    def __call__(self, image, masks=None, boxes=None, labels=None):
        image[:, :, 0] += random.uniform(-self.delta, self.delta)
        image[:, :, 0][image[:, :, 0] > 360.0] -= 360.0
        image[:, :, 0][image[:, :, 0] < 0.0] += 360.0
        return image, masks, boxes, labels


class ToCV2Image(object):
    def __call__(self, tensor, masks=None, boxes=None, labels=None):
        return tensor.cpu().numpy().astype(np.float32).transpose((1, 2, 0)), masks, boxes, labels


class ToTensor(object):
    def __call__(self, cvimage, masks=None, boxes=None, labels=None):
        return torch.from_numpy(cvimage.astype(np.float32)).permute(2, 0, 1), masks, boxes, labels


# TODO: this seems not good, fix it later.
class RandomSampleCrop(object):
    # Potentialy sample a random crop from the image and put it in a random place
    """Crop
    Arguments:
        img (Image): the image being input during training
        boxes (Tensor): the original bounding boxes in pt form
        labels (Tensor): the class labels for each bbox
        mode (float tuple): the min and max jaccard overlaps
    Return:
        (img, boxes, classes)
            img (Image): the cropped image
            boxes (Tensor): the adjusted bounding boxes in pt form
            labels (Tensor): the class labels for each bbox
    """

    def __init__(self):
        self.sample_options = (
            # using entire original input image
            None,
            # sample a patch s.t. MIN jaccard w/ obj in .1,.3,.4,.7,.9
            (0.1, None),
            (0.3, None),
            (0.7, None),
            (0.9, None),
            # randomly sample a patch
            (None, None))

    def __call__(self, image, masks, boxes=None, labels=None):
        height, width, _ = image.shape
        while True:
            # randomly choose a mode
            mode = random.choice(self.sample_options)
            if mode is None:
                return image, masks, boxes, labels

            min_iou, max_iou = mode
            if min_iou is None:
                min_iou = float('-inf')
            if max_iou is None:
                max_iou = float('inf')

            # max trails (50)
            for _ in range(50):
                current_image = image

                w = random.uniform(0.3 * width, width)
                h = random.uniform(0.3 * height, height)

                # aspect ratio constraint b/t .5 & 2
                if h / w < 0.5 or h / w > 2:
                    continue

                left = random.uniform(width - w)
                top = random.uniform(height - h)

                # convert to integer rect x1,y1,x2,y2
                rect = np.array([int(left), int(top), int(left + w), int(top + h)])

                # calculate IoU (jaccard overlap) b/t the cropped and gt boxes
                overlap = jaccard_numpy(boxes, rect)

                # This piece of code is bugged and does nothing: https://github.com/amdegroot/ssd.pytorch/issues/68
                #
                # However, when I fixed it with overlap.max() < min_iou,
                # it cut the mAP in half (after 8k iterations). So it stays.
                #
                # is min and max overlap constraint satisfied? if not try again
                if overlap.min() < min_iou and max_iou < overlap.max():
                    continue

                # cut the crop from the image
                current_image = current_image[rect[1]:rect[3], rect[0]:rect[2], :]

                # keep overlap with gt box IF center in sampled patch
                centers = (boxes[:, :2] + boxes[:, 2:]) / 2.0

                # mask in all gt boxes that above and to the left of centers
                m1 = (rect[0] < centers[:, 0]) * (rect[1] < centers[:, 1])

                # mask in all gt boxes that under and to the right of centers
                m2 = (rect[2] > centers[:, 0]) * (rect[3] > centers[:, 1])

                # mask in that both m1 and m2 are true
                mask = m1 * m2

                # [0 ... 0 for num_gt and then 1 ... 1 for num_crowds]
                num_crowds = labels['num_crowds']
                crowd_mask = np.zeros(mask.shape, dtype=np.int32)

                if num_crowds > 0:
                    crowd_mask[-num_crowds:] = 1

                # have any valid boxes? try again if not
                # Also make sure you have at least one regular gt
                if not mask.any() or np.sum(1 - crowd_mask[mask]) == 0:
                    continue

                # take only the matching gt masks
                current_masks = masks[mask, :, :].copy()

                # take only matching gt boxes
                current_boxes = boxes[mask, :].copy()

                # take only matching gt labels
                labels['labels'] = labels['labels'][mask]
                current_labels = labels

                # We now might have fewer crowd annotations
                if num_crowds > 0:
                    labels['num_crowds'] = np.sum(crowd_mask[mask])

                # should we use the box left and top corner or the crop's
                current_boxes[:, :2] = np.maximum(current_boxes[:, :2], rect[:2])
                # adjust to crop (by substracting crop's left,top)
                current_boxes[:, :2] -= rect[:2]

                current_boxes[:, 2:] = np.minimum(current_boxes[:, 2:], rect[2:])
                # adjust to crop (by substracting crop's left,top)
                current_boxes[:, 2:] -= rect[:2]

                # crop the current masks to the same dimensions as the image
                current_masks = current_masks[:, rect[1]:rect[3], rect[0]:rect[2]]

                return current_image, current_masks, current_boxes, current_labels  # This


class Expand(object):
    # Have a chance to scale down the image and pad (to emulate smaller detections)
    def __init__(self):
        pass

    def __call__(self, image, masks, boxes, labels):
        if random.randint(2):
            return image, masks, boxes, labels

        height, width, depth = image.shape
        ratio = random.uniform(1, 1.8)
        left = random.uniform(0, width * ratio - width)
        top = random.uniform(0, height * ratio - height)

        expand_image = np.random.rand(int(height * ratio), int(width * ratio), depth) * 255
        expand_image[int(top):int(top + height), int(left):int(left + width)] = image
        image = expand_image

        expand_masks = np.zeros((masks.shape[0], int(height * ratio), int(width * ratio)), dtype=masks.dtype)
        expand_masks[:, int(top):int(top + height), int(left):int(left + width)] = masks
        masks = expand_masks

        boxes = boxes.copy()
        boxes[:, :2] += (int(left), int(top))
        boxes[:, 2:] += (int(left), int(top))

        return image, masks, boxes, labels


class RandomMirror(object):
    # Mirror the image with a probability of 1/2
    def __call__(self, image, masks, boxes, labels):
        _, width, _ = image.shape
        if random.randint(2):
            image = image[:, ::-1]
            masks = masks[:, :, ::-1]
            boxes = boxes.copy()
            boxes[:, 0::2] = width - boxes[:, 2::-2]
        return image, masks, boxes, labels


class PhotometricDistort(object):
    def __init__(self):
        # RandomContrast() and RandomBrightness() do not influence the normalize result if they are behind of
        # RandomSaturation() and RandomHue().
        self.pd = [RandomContrast(),
                   RandomBrightness(),
                   ConvertColor(transform='HSV'),
                   RandomSaturation(),
                   RandomHue(),
                   ConvertColor(current='HSV', transform='BGR')]

    def __call__(self, image, masks, boxes, labels):
        if random.randint(2):
            distort = Compose(self.pd)
            image, masks, boxes, labels = distort(image, masks, boxes, labels)
        return image, masks, boxes, labels


class Normalize(object):
    def __init__(self):
        pass

    def __call__(self, img, masks=None, boxes=None, labels=None):
        img = img.astype(np.float32)
        # TODO: check if this right
        for i in range(3):
            img[:, :, i] = (img[:, :, i] - np.mean(img[:, :, i])) / (np.std(img[:, :, i]))
        # TODO: do this alone
        img = img[:, :, (2, 1, 0)]  # TO RGB

        return img.astype(np.float32), masks, boxes, labels


class BaseTransform(object):
    """ Transorm to be used when evaluating. """

    def __init__(self):
        self.augment = Compose([ImgToFloat(),
                                Resize(resize_gt=False),
                                Normalize()])

    def __call__(self, img, masks=None, boxes=None, labels=None):
        return self.augment(img, masks, boxes, labels)


class FastBaseTransform(torch.nn.Module):
    """
    Transform that does all operations on the GPU for super speed.
    This doesn't suppport a lot of config settings and should only be used for production.
    Maintain this as necessary.
    """

    def __init__(self):
        super().__init__()

        self.mean = torch.Tensor(MEANS).float()[None, :, None, None]
        self.std = torch.Tensor(STD).float()[None, :, None, None]
        self.transform = cfg.backbone.transform

    def forward(self, img):
        self.mean = self.mean.to(img.device)
        self.std = self.std.to(img.device)

        # img assumed to be a pytorch BGR image with channel order [n, h, w, c]
        img = img.permute(0, 3, 1, 2).contiguous()
        img = F.interpolate(img, (cfg.img_size, cfg.img_size), mode='bilinear', align_corners=False)

        if self.transform.normalize:
            img = (img - self.mean) / self.std
        elif self.transform.subtract_means:
            img = (img - self.mean)
        elif self.transform.to_float:
            img = img / 255

        if self.transform.channel_order != 'RGB':
            raise NotImplementedError

        img = img[:, (2, 1, 0), :, :].contiguous()

        # Return value is in channel order [n, c, h, w] and RGB
        return img


class SSDAugmentation(object):
    def __init__(self):
        self.augment = Compose([ImgToFloat(),
                                PhotometricDistort(),  # 50% possibility
                                ToAbsoluteBox(),
                                RandomSampleCrop(),
                                Expand(),  # 50% possibility
                                RandomMirror(),
                                Resize(),
                                ToPercentBox(),
                                Normalize()])

    def __call__(self, img, masks, boxes, labels):
        # aa = self.augment(img, masks, boxes, labels)
        # img = aa[0].astype('uint8')
        # bb = aa[2].astype('int').tolist()
        #
        # for one_bb in bb:
        #     cv2.rectangle(img, (one_bb[0], one_bb[1]), (one_bb[2], one_bb[3]), (0, 255, 0), 1)
        #
        # cv2.imshow('aa', img)
        # cv2.waitKey()

        return self.augment(img, masks, boxes, labels)
