import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.parameter import Parameter
import math
'''
支持  AMClassifier,ArcClassifier,CircleClassifier
'''
class FocalLoss(nn.Module):
    def __init__(self, gamma=0, eps=1e-7):
        super(FocalLoss, self).__init__()
        self.gamma = gamma
        self.eps = eps
        self.ce = torch.nn.CrossEntropyLoss()
    def forward(self, input, target):
        logp = self.ce(input, target)
        p = torch.exp(-logp)
        loss = (1 - p) ** self.gamma * logp
        return loss.mean()

class AMClassifier(nn.Module):
    '''
    在余弦相似度上加入间隔，加大类别间的区分能力
    AMSoftmax loss= cosθ - m
    '''
    def __init__(self, cfg):
        super(AMClassifier, self).__init__()
        self._feature_dim = cfg.feature_dim # 特征维数 128/512
        self._class_num = cfg.train_class_range[1]- cfg.train_class_range[0] # 类别总数
        self._scale = cfg.scale  # 特征模长缩放因子 论文推荐为30
        self._margin = cfg.margin  # 类别间隔  论文中表现好的是0.35~0.4
        self._criterion = FocalLoss(gamma=2)  # Focal Loss

        # 类别代理矩阵 [class_num,feature_dim]
        self.register_parameter(name='id_agent', param=Parameter(torch.Tensor(self._class_num , self._feature_dim)))

        # 初始化类别代理矩阵
        stdv = 1. / math.sqrt(self.id_agent.size(1))
        self.id_agent.data.uniform_(-stdv, stdv)

    def forward(self,inputs):
        # x [batch,class_num]   target[batch]
        x, target = inputs
        # x id_agent进行L2规范化，使得向量模长为1
        x_normalized = F.normalize(x, p=2, dim=1)
        self.id_agent.data = F.normalize(self.id_agent.data, p=2, dim=1)
        # 向量乘法公式(x→)*(y→) = |x| |y| cos θ   由于x,y模长为1，则结果为 特征与对应类中心夹角的余弦值cos θ
        # 余弦相似度矩阵 [batch,feature_dim]
        score = F.linear(x_normalized, self.id_agent)

        # 训练时加入类别间隔margin，即将余弦相似度矩阵内 当前batch对应的相似度减去间隔
        if self.training and self._margin > 0:
            index_sample = torch.arange(0, x.shape[0]).long() # 真值标签的下标 [batch]
            index_class = target.view(-1).long()   # 真值标签，即所属类别 [batch]
            score[index_sample, index_class] = score[index_sample, index_class] - self._margin

        # AM提出尺度因子s，转化到 半径为s的超球面
        score = score * self._scale

        # 交叉熵损失
        # loss_softmax = F.nll_loss(F.log_softmax(score, dim=1), target)
        # losses = loss_softmax.unsqueeze(dim=0)

        # Focal Loss损失
        # score[batch，class_num]   target[batch]
        losses = self._criterion(score, target).unsqueeze(dim=0)
        return losses

class ArcClassifier(nn.Module):
    """
    弧长度量相似度
    在余弦角度上加入间隔，加大类别间的区分能力
    ArcFace = cos(θ+ m)
    """

    def __init__(self,cfg,easy_margin = False):
        '''
        :param easy_margin:   True:ArcFace,cos(θ+m)    False:ArcFace结合AMSoftmax,cos(θ+m1)-m2
        '''
        super(ArcClassifier, self).__init__()
        self._feature_dim = cfg.feature_dim # 特征维数 128/512
        self._class_num = cfg.train_class_range[1] - cfg.train_class_range[0]  # 类别总数
        self._criterion = FocalLoss(gamma=2)  # Focal Loss
        self.s = cfg.scale # 特征模长缩放因子
        self.m = cfg.margin  # 类别间隔

        # 类别代理矩阵 [class_num,feature_dim]
        self.id_agent = Parameter(torch.FloatTensor(self._class_num, self._feature_dim))
        nn.init.normal_(self.id_agent, std=0.01)

        self.easy_margin = easy_margin
        self.cos_m = math.cos(self.m)
        self.sin_m = math.sin(self.m)
        self.th = math.cos(math.pi - self.m)
        self.mm = math.sin(math.pi - self.m) * self.m

    def forward(self, inputs):
        # embbedings [batch,class_num]   label[batch]
        embbedings, label=inputs

        # embbedings id_agent进行L2规范化，使得向量模长为1
        embbedings = F.normalize(embbedings, p=2, dim=1)
        kernel_norm = F.normalize(self.id_agent, p=2, dim=1)

        # 向量乘法公式(x→)*(y→) = |x| |y| cos θ   由于x,y模长为1，则结果为 特征与对应类中心夹角的余弦值cos θ
        # 余弦相似度矩阵 [batch,feature_dim]
        cos_theta = F.linear(embbedings, kernel_norm)
        cos_theta = cos_theta.clamp(-1, 1)  # 为数值稳定，余弦值位于[-1,1]范围


        # 从余弦相似度矩阵cos_theta 取出  当前batch对应类的预测余弦值
        target_logit = cos_theta[torch.arange(0, embbedings.size(0)), label].view(-1, 1)
        # cosθ 加入间隔m，得到cos(θ+m)
        sin_theta = torch.sqrt(1.0 - torch.pow(target_logit, 2)) # 定理 sin⑵θ+cos⑵θ=1
        cos_theta_m = target_logit * self.cos_m - sin_theta * self.sin_m # 定理 cos(θ+m)=cosθcosm - sin θsinm
        if self.easy_margin:
            # ArcFace
            final_target_logit = torch.where(target_logit > 0, cos_theta_m, target_logit)
        else:
            # ArcFace + AMSoftmax
            final_target_logit = torch.where(target_logit > self.th, cos_theta_m, target_logit - self.mm)

        # 将加入间隔的余弦值放回 相似度矩阵
        cos_theta.scatter_(1, label.view(-1, 1).long(), final_target_logit)
        # 转化到 半径为s的超球面
        output = cos_theta * self.s
        # Focal loss损失
        losses = self._criterion(output, label).unsqueeze(dim=0)
        return losses

class CircleClassifier(nn.Module):
    '''
    在余弦角加入带权重的间隔，加大类别间的区分能力(CVPR2020 Oral)
    CircleLoss= cos(α*(Sn-m1)+β*(Sp-m2))    α,β是权重   m1,m2是间隔margin
    参考 https://github.com/cavalleria/cavaface.pytorch/blob/master/head/metrics.py
    '''

    def __init__(self, cfg):
        super(CircleClassifier, self).__init__()
        self._margin = cfg.margin # 类别间隔
        self._scale = cfg.scale # 特征模长缩放因子
        self._class_num = cfg.train_class_range[1]- cfg.train_class_range[0] # 类别总数
        self._feature_dim = cfg.feature_dim # 特征维数 128/512
        self.soft_plus = nn.Softplus() # Softmax激活函数

        # 类别代理矩阵 [class_num,feature_dim]
        self.id_agent = nn.Parameter(torch.FloatTensor(self._class_num, self._feature_dim))
        nn.init.xavier_uniform_(self.id_agent)

    def forward(self, inputs):
        # x [batch,class_num]   target[batch]
        x, target = inputs
        # x id_agent进行L2规范化，使得向量模长为1
        # 向量乘法公式(x→)*(y→) = |x| |y| cos θ   由于x,y模长为1，则结果为 特征与对应类中心夹角的余弦值cos θ
        # 余弦相似度矩阵 [batch,feature_dim]
        similarity_matrix = F.linear(F.normalize(x, p=2, dim=1), F.normalize(self.id_agent, p=2, dim=1))


        # [batch，class_num]
        one_hot = torch.zeros_like(similarity_matrix)
        one_hot.scatter_(1, target.view(-1, 1).long(), 1)
        # 0,1转为False,True
        one_hot = one_hot.type(dtype=torch.bool)

        #  得到每个样本（每行）的类内距离，即与对应类中心的余弦值 [batch,1]
        sp = similarity_matrix[one_hot]
        mask = one_hot.logical_not() # logical_not逻辑非， False,True取反
        # 得到每个样本（每行）的类间距离， 即与 其余类别的类中心余弦值 [batch,类别数-1]
        sn = similarity_matrix[mask]

        sp = sp.view(x.size()[0], -1)
        sn = sn.view(x.size()[0], -1)

        # 论文公式(5)(8) 根据余弦值确定权重ap an   clamp_min确保非负
        ap = torch.clamp_min(-sp.detach() + 1 + self._margin, min=0.)
        an = torch.clamp_min(sn.detach() + self._margin, min=0.)

        # 公式(8)  delta_p,delta_n是类内及类间间隔m1,m2
        delta_p = 1 - self._margin
        delta_n = self._margin

        # 公式(6)  exp(*)括号内的运算
        logit_p = - ap * (sp - delta_p) * self._scale
        logit_n = an * (sn - delta_n) * self._scale

        # 公式(6)  softplus激活函数= ln[1+exp(x)]
        loss = self.soft_plus(torch.logsumexp(logit_n, dim=1) + torch.logsumexp(logit_p, dim=1))
        return loss.mean()



