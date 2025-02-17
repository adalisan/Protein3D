import numpy as np
import torch

from torch import nn
from torch.nn import functional as F

from equivariant_attention.modules import GConvSE3, GNormSE3, get_basis_and_r, GSE3Res, GMaxPooling, GAvgPooling
from equivariant_attention.fibers import Fiber

import pytorch_lightning as pl
import torchmetrics as tm

from datasets import *

EPS = 1e-13

# ##################### Hyperpremeter Setting #########################
class ExpSetting(object):
    def __init__(self, distance_cutoff=[3, 3.5], data_address='../data/ProtFunct.pt', log_file=None, log_dir = 'log/', batch_size=4, lr=1e-3, num_epochs=2, num_workers=4, num_layers=2, num_degrees=3, num_channels=20, num_nlayers=0, pooling='avg', head=1, div=4, seed=0, num_class=384, use_classes=None, hyperparameter=None, decoder_mid_dim=60): 
        self.distance_cutoff = distance_cutoff
        self.data_address = data_address
        self.log_file = log_file
        self.log_dir = log_dir
        self.hyperparameter = hyperparameter

        self.batch_size = batch_size
        self.lr = lr          		 	  # learning rate
        self.num_epochs = num_epochs          
        self.num_workers = num_workers

        self.num_layers = num_layers      # number of equivariant layer
        self.num_degrees = num_degrees    # number of irreps {0,1,...,num_degrees-1}
        self.num_channels = num_channels  # number of channels in middle layers
        self.num_nlayers = num_nlayers    # number of layers for nonlinearity
        self.pooling = pooling        	  # choose from avg or max
        self.head = head                  # number of attention heads
        self.div = div                    # low dimensional embedding fraction
        self.decoder_mid_dim = decoder_mid_dim

        self.num_class = num_class        # number of class in multi-class decoder
        self.use_classes = use_classes

        self.seed = seed                  # random seed for both numpy and pytorch

        self.n_bounds = len(distance_cutoff) + 1
        
        self.device = torch.device('cuda:0') if torch.cuda.is_available() else torch.device('cpu')				 # Automatically choose GPU if available


class TFN(nn.Module):
    """SE(3) equivariant GCN"""
    def __init__(self, num_layers: int, atom_feature_size: int, 
                num_channels: int, num_nlayers: int=1, num_degrees: int=4, 
                edge_dim: int=4, **kwargs):
        super().__init__()
        # Build the network
        self.num_layers = num_layers
        self.num_nlayers = num_nlayers
        self.num_channels = num_channels
        self.num_degrees = num_degrees
        self.num_channels_out = num_channels*num_degrees
        self.edge_dim = edge_dim

        self.fibers = {'in': Fiber(1, atom_feature_size),
                    'mid': Fiber(num_degrees, self.num_channels),
                    'out': Fiber(1, self.num_channels_out)}

        blocks = self._build_gcn(self.fibers, 1)
        self.block0, self.block1, self.block2 = blocks
        print(self.block0)
        print(self.block1)
        print(self.block2)

    def _build_gcn(self, fibers, out_dim):

        block0 = []
        fin = fibers['in']
        for i in range(self.num_layers-1):
            block0.append(GConvSE3(fin, fibers['mid'], self_interaction=True, edge_dim=self.edge_dim))
            block0.append(GNormSE3(fibers['mid'], num_layers=self.num_nlayers))
            fin = fibers['mid']
        block0.append(GConvSE3(fibers['mid'], fibers['out'], self_interaction=True, edge_dim=self.edge_dim))

        block1 = [GMaxPooling()]

        block2 = []
        block2.append(nn.Linear(self.num_channels_out, self.num_channels_out))
        block2.append(nn.ReLU(inplace=True))
        block2.append(nn.Linear(self.num_channels_out, out_dim))

        return nn.ModuleList(block0), nn.ModuleList(block1), nn.ModuleList(block2)

    def forward(self, G):
        # Compute equivariant weight basis from relative positions
        basis, r = get_basis_and_r(G, self.num_degrees-1)

        # encoder (equivariant layers)
        h = {'0': G.ndata['f']}
        for layer in self.block0:
            h = layer(h, G=G, r=r, basis=basis)

        h = h['0'][...,-1]
        for layer in self.block1:
            h = layer(G, h)

        for layer in self.block2:
            h = layer(h)

        return h


class SE3Transformer(nn.Module):
    """SE(3) equivariant GCN with attention"""
    def __init__(self, num_layers: int, atom_feature_size: int, 
                num_channels: int, num_nlayers: int=1, num_degrees: int=4, 
                edge_dim: int=4, div: float=4, pooling: str='avg', n_heads: int=1, **kwargs):
        super().__init__()
        # Build the network
        self.num_layers = num_layers
        self.num_nlayers = num_nlayers
        self.num_channels = num_channels
        self.num_degrees = num_degrees
        self.edge_dim = edge_dim
        self.div = div
        self.pooling = pooling
        self.n_heads = n_heads

        self.fibers = {'in': Fiber(1, atom_feature_size),
                    'mid': Fiber(num_degrees, self.num_channels),
                    'out': Fiber(1, num_degrees*self.num_channels)}

        blocks = self._build_gcn(self.fibers, 1)
        self.Gblock, self.FCblock = blocks
        print(self.Gblock)
        print(self.FCblock)

    def _build_gcn(self, fibers, out_dim):
        # Equivariant layers
        Gblock = []
        fin = fibers['in']
        for i in range(self.num_layers):
            Gblock.append(GSE3Res(fin, fibers['mid'], edge_dim=self.edge_dim, 
                                div=self.div, n_heads=self.n_heads))
            Gblock.append(GNormSE3(fibers['mid']))
            fin = fibers['mid']
        Gblock.append(GConvSE3(fibers['mid'], fibers['out'], self_interaction=True, edge_dim=self.edge_dim))

        # Pooling
        if self.pooling == 'avg':
            Gblock.append(GAvgPooling())
        elif self.pooling == 'max':
            Gblock.append(GMaxPooling())

        # FC layers
        FCblock = []

        FCblock.append(nn.Linear(self.fibers['out'].n_features, self.fibers['out'].n_features))
        FCblock.append(nn.ReLU(inplace=True))
        FCblock.append(nn.Linear(self.fibers['out'].n_features, out_dim))

        return nn.ModuleList(Gblock), nn.ModuleList(FCblock)

    def forward(self, G):
        # Compute equivariant weight basis from relative positions
        basis, r = get_basis_and_r(G, self.num_degrees-1)

        # encoder (equivariant layers)
        h = {'0': G.ndata['f']}
        for layer in self.Gblock:
            h = layer(h, G=G, r=r, basis=basis)

        for layer in self.FCblock:
            h = layer(h)

        return h


class SE3TransformerEncoder(nn.Module):
    """SE(3) equivariant GCN with attention"""
    def __init__(self, num_layers: int, atom_feature_size: int, 
                num_channels: int, num_nlayers: int=1, num_degrees: int=4, 
                edge_dim: int=4, div: float=4, pooling: str='avg', n_heads: int=1, **kwargs):
        super().__init__()
        # Build the network
        self.num_layers = num_layers
        self.num_nlayers = num_nlayers
        self.num_channels = num_channels
        self.num_degrees = num_degrees
        self.edge_dim = edge_dim
        self.div = div
        self.pooling = pooling
        self.n_heads = n_heads

        self.fibers = {'in': Fiber(1, atom_feature_size),
                    'mid': Fiber(num_degrees, self.num_channels),
                    'out': Fiber(1, num_degrees*self.num_channels)}

        self.Gblock = self._build_gcn(self.fibers, 1)
        # print(self.Gblock)

    def _build_gcn(self, fibers, out_dim):
        # Equivariant layers
        Gblock = []
        fin = fibers['in']
        for i in range(self.num_layers):
            Gblock.append(GSE3Res(fin, fibers['mid'], edge_dim=self.edge_dim, 
                                div=self.div, n_heads=self.n_heads))
            Gblock.append(GNormSE3(fibers['mid']))
            fin = fibers['mid']
        Gblock.append(GConvSE3(fibers['mid'], fibers['out'], self_interaction=True, edge_dim=self.edge_dim))

        # Pooling
        if self.pooling == 'avg':
            Gblock.append(GAvgPooling())
        elif self.pooling == 'max':
            Gblock.append(GMaxPooling())

        return nn.ModuleList(Gblock)

    def forward(self, G):
        # Compute equivariant weight basis from relative positions
        basis, r = get_basis_and_r(G, self.num_degrees-1)

        # encoder (equivariant layers)
        h = {'0': G.ndata['f']}
        for layer in self.Gblock:
            h = layer(h, G=G, r=r, basis=basis)

        return h


class MultiClassInnerProductLayer(nn.Module):
    def __init__(self, in_dim, num_class):
        super(MultiClassInnerProductLayer, self).__init__()
        self.num_class = num_class
        self.in_dim = in_dim

        self.embedding = nn.Parameter(torch.Tensor(self.in_dim, self.num_class))

        self.reset_parameters()

    def __repr__(self):
        return f'MultiClassInnerProductLayer(structure=[(batch_size, {self.num_class})]'

    def forward(self, z, softmax=False):
        # value = (z[node_list] * self.weight[node_label]).sum(dim=1)
        # value = torch.sigmoid(value) if sigmoid else value

        pred = torch.matmul(z, self.embedding)
        pred = torch.softmax(pred, dim=1) if softmax else pred

        return pred

    def reset_parameters(self):
        stdv = np.sqrt(6.0 / (self.embedding.size(-2) + self.embedding.size(-1)))
        self.embedding.data.uniform_(-stdv, stdv)
        # self.weight.data.normal_()


class ProtBinary(pl.LightningModule):
    def __init__(self, setting: ExpSetting, pred_class_binary=3):
        super().__init__()
        self.setting = setting
        self.pred_class = pred_class_binary
        self.model = SE3Transformer(setting.num_layers, len(residue2idx), setting.num_channels, setting.num_nlayers, setting.num_degrees, edge_dim=3, n_bonds=setting.n_bounds, div=setting.div, pooling=setting.pooling, head=setting.head)

    def forward(self, g):
        """get model prediction"""
        prob = self._run_step(g)
        return prob

    def _run_step(self, g):
        """compute forward"""
        z = self.model(g)
        return torch.sigmoid(z)

    def step(self, batch, batch_idx):
        # print(batch_idx)
        g, y_org, pdb = batch
              
        y = torch.zeros(y_org.shape[0])
        y[y_org==self.pred_class] = 1

        pred = self._run_step(g)

        y = y.type_as(pred)

        l1_loss = torch.sum(torch.abs(pred - y))
        l2_loss = torch.sum((pred - y)**2)
        
        # if use_mean:
        l1_loss /= pred.shape[0]
        l2_loss /= pred.shape[0]		

        loss = l1_loss

        logs = {
            "l1_loss": l1_loss,
            "l2_loss": l2_loss,
        }
        return loss, logs

    def training_step(self, batch, batch_idx):
        loss, logs = self.step(batch, batch_idx)
        self.log_dict({f"train_{k}": v for k, v in logs.items()}, on_step=True, on_epoch=False)
        return loss

    def validation_step(self, batch, batch_idx):
        loss, logs = self.step(batch, batch_idx)
        self.log_dict({f"val_{k}": v for k, v in logs.items()})
        return loss

    def test_step(self, batch, batch_idx):
        loss, logs = self.step(batch, batch_idx)
        self.log_dict({f"val_{k}": v for k, v in logs.items()})
        return loss

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), self.setting.lr)
        scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(optimizer, self.setting.num_epochs, eta_min=1e-4)
        return [optimizer], [scheduler]


# class ProtMultClass(pl.LightningModule):
#     def __init__(self, setting: ExpSetting):
#         super().__init__()
#         self.setting = setting
#         self.__setup_log(setting.log_file)

#         self.model = self.__build_model()

#         self.max_steps = 3
#         print(self.model)

#     def __setup_log(self, file_name):
#         self.log_step_file = f'{file_name}_step.txt'
#         self.log_epoch_file = f'{file_name}_epoch.txt'

#         self.write_to_step_log(',step_loss\n')
#         self.write_to_epoch_log(',epoch_loss_valid,epoch_loss_train\n')
        
#     def __build_model(self):
#         model = []

#         model.append(SE3TransformerEncoder(self.setting.num_layers, len(residue2idx), self.setting.num_channels, self.setting.num_nlayers, self.setting.num_degrees, edge_dim=3, n_bonds=self.setting.n_bounds, div=self.setting.div, pooling=self.setting.pooling, head=self.setting.head))

#         mid_dim = model[0].fibers['out'].n_features

#         model.append(nn.Linear(mid_dim, mid_dim))
#         model.append(nn.ReLU(inplace=True))

#         model.append(MultiClassInnerProductLayer(mid_dim, self.setting.num_class))

#         return nn.ModuleList(model)

#     def forward(self, g):
#         """get model prediction"""
#         prob = self._run_step(g)

#         return prob

#     def _run_step(self, g):
#         """compute forward"""
#         z = g
#         for layer in self.model:
#             z = layer(z)

#         return torch.sigmoid(z)

#     def __to_onehot(self, y_list):
#         # convert class number to onehot representation

#         return F.one_hot(y_list, num_classes=self.setting.num_class)

#     def step(self, batch, batch_idx):
#         # print(batch_idx)
#         g, y_org, pdb = batch

#         pred = self._run_step(g)
#         y = self.__to_onehot(y_org).type_as(pred)

#         pos_pred = pred * y

#         l1_loss = torch.sum(torch.abs(pred - y))  + torch.sum(torch.abs(pos_pred - y)) * pred.shape[1]
#         # l2_loss = torch.sum((pred - y)**2) + torch.sum((pos_pred - y)**2) * pred.shape[1]
        
#         # if use_mean:
#         # l1_loss /= pred.shape[0]
#         # l2_loss /= pred.shape[0]		

#         loss = l1_loss

#         logs = {
#             "l1_loss": l1_loss.to('cpu'),
#             # "l2_loss": l2_loss.to('cpu'),
#         }
#         return loss, logs

#     def training_step(self, batch, batch_idx):
#         loss, logs = self.step(batch, batch_idx)
#         self.log_dict({f"train_{k}": v for k, v in logs.items()}, on_step=True, on_epoch=True)

#         self.write_to_step_log(f',{loss:.4f}\n')
#         # print('train ', loss)

#         return loss

#     def training_epoch_end(self, outputs: list) -> None:
#         epoch_loss = torch.stack([x["loss"] for x in outputs]).mean()
#         self.write_to_epoch_log(f',{epoch_loss:.4f}\n')

#     def validation_step(self, batch, batch_idx):
#         loss, logs = self.step(batch, batch_idx)
#         self.log_dict({f"val_{k}": v for k, v in logs.items()})

#         # print('valid ', loss)

#         return loss
    
#     def validation_epoch_end(self, outputs: list) -> None:
#         epoch_loss = torch.stack(outputs).mean()
#         self.write_to_epoch_log(f',{epoch_loss:.4f}')

#     def test_step(self, batch, batch_idx):
#         loss, logs = self.step(batch, batch_idx)
#         self.log_dict({f"test_{k}": v for k, v in logs.items()}, on_step=False, on_epoch=True)

#         return loss

#     def _load_data(self, mode='train'):
#         dataset = ProtFunctDataset(
#             self.setting.data_address, 
#             mode=mode, 
#             if_transform=True, 
#             dis_cut=self.setting.distance_cutoff)

#         loader = DataLoader(
#             dataset, 
#             batch_size=self.setting.batch_size, 
#             shuffle=False, 
#             collate_fn=collate, 
#             num_workers=self.setting.num_workers)

#         return loader

#     def train_dataloader(self):

#         return self._load_data(mode='train')

#     def val_dataloader(self):

#         return self._load_data(mode='valid')

#     def test_dataloader(self):

#         return self._load_data(mode='test')

#     def configure_optimizers(self):
#         optimizer = torch.optim.Adam(self.parameters(), self.setting.lr)
#         scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(
#             optimizer, 
#             self.setting.num_epochs, 
#             eta_min=1e-4)

#         return [optimizer], [scheduler]

#     def write_to_step_log(self, context: str):
#         with open(self.log_step_file, 'a') as f:
#             f.write(f'{context}')

#     def write_to_epoch_log(self, context: str):
#         with open(self.log_epoch_file, 'a') as f:
#             f.write(f'{context}')    


class ProtMultClass(pl.LightningModule):
    def __init__(self, setting: ExpSetting):
        super().__init__()
        self.setting = setting
        self.__setup_log(setting.log_dir)
        self.__setup_matrices()
        self.__setup_loss()

        self.model = self.__build_model()

    def __setup_loss(self):
        # self.loss_function = torch.nn.NLLLoss()
        self.loss_function = torch.nn.CrossEntropyLoss()

    def __setup_matrices(self):
        metrics = tm.MetricCollection([tm.Accuracy(num_classes=self.setting.num_class)
        # , tm.AUROC(num_classes=self.setting.num_class, compute_on_step=False)
        ])
        self.metrics_dict = {}
        self.metrics_dict['train'] = metrics.clone(prefix='train_')
        self.metrics_dict['valid'] = metrics.clone(prefix='valid_')
        self.metrics_dict['test'] = metrics.clone(prefix='test_')


    def __setup_log(self, file_dir):
        if not os.path.exists(file_dir):
            os.makedirs(file_dir)
        
        # log file paths
        self.log_step_file = os.path.join(file_dir, 'step.txt')
        self.log_epoch_file = os.path.join(file_dir, 'epoch.txt')
        self.log_test_file = os.path.join(file_dir, 'test.txt')
        self.log_valid_file = os.path.join(file_dir, 'valid.txt')

        # write head
        self.write_to_step_log('step_loss\n')
        self.write_to_valid_log('step_loss\n')
        self.write_to_epoch_log(',loss_valid,acc_valid,auroc_valid,loss_train,acc_train, auroc_train\n')

        # log hyperparameter
        torch.save(self.setting, os.path.join(file_dir, 'setting.pt'))

    def __build_model(self):
        model = []

        model.append(SE3TransformerEncoder(self.setting.num_layers, len(residue2idx), self.setting.num_channels, self.setting.num_nlayers, self.setting.num_degrees, edge_dim=3, n_bonds=self.setting.n_bounds, div=self.setting.div, pooling=self.setting.pooling, head=self.setting.head))

        mid_dim = model[0].fibers['out'].n_features

        model.append(nn.Linear(mid_dim, self.setting.decoder_mid_dim))
        model.append(nn.ReLU(inplace=True))

        model.append(MultiClassInnerProductLayer(self.setting.decoder_mid_dim, self.setting.num_class))

        return nn.ModuleList(model)

    def forward(self, g):
        """get model prediction"""
        prob = self._run_step(g)

        return prob

    def _run_step(self, g, if_sigmoid=True):
        """compute forward"""
        z = g
        for layer in self.model:
            z = layer(z)
        if if_sigmoid:
            z = torch.sigmoid(z)
        return z

    def __to_onehot(self, y_list):
        # convert class number to onehot representation

        return F.one_hot(y_list, num_classes=self.setting.num_class)

    def __compute_epoch_metrics(self, mode):
        outputs = self.metrics_dict[mode].compute()
        self.metrics_dict[mode].reset()

        return outputs

    def step(self, batch, mode='train'):
        # print(batch_idx)
        g, targets, pdb = batch
        
        preds = self._run_step(g)

        loss = self.loss_function(preds, targets)

        outputs = self.metrics_dict[mode](preds, targets)
        outputs[f'{mode}_loss'] = loss

        return loss, outputs

    def training_step(self, batch, batch_idx):
        loss, outputs = self.step(batch, mode='train')

        self.log_dict(outputs, on_step=True, on_epoch=True)
        self.write_to_step_log(f',{loss:.4f}\n')

        return loss

    def training_epoch_end(self, outputs: list) -> None:
        epoch_loss = torch.stack([x["loss"] for x in outputs]).mean()
        outputs = self.__compute_epoch_metrics('train')

        self.write_to_epoch_log(f",{epoch_loss:.4f}, {outputs['train_Accuracy']:.4f}, 0\n")
        # self.write_to_epoch_log(f",{epoch_loss:.4f}, {outputs['train_Accuracy']:.4f}, {outputs['train_AUROC']:.4f}\n")
        print(f"train --> loss: {epoch_loss:.4f}, acc: {outputs['train_Accuracy']:.4f}")


    def validation_step(self, batch, batch_idx):
        loss, outputs = self.step(batch, 'valid')
        outputs['valid_loss'] = loss

        self.log_dict(outputs, on_step=True, on_epoch=True)
        self.write_to_valid_log(f'{loss:.4f}\n')

        return loss
    
    def validation_epoch_end(self, outputs: list) -> None:
        epoch_loss = torch.stack(outputs).mean()
        outputs = self.__compute_epoch_metrics('valid')
        print(f"valid --> loss: {epoch_loss:.4f}, acc: {outputs['valid_Accuracy']:.4f}")

        self.write_to_epoch_log(f",{epoch_loss:.4f}, {outputs['valid_Accuracy']:.4f}, 0")
        # self.write_to_epoch_log(f",{epoch_loss:.4f}, {outputs['valid_Accuracy']:.4f}, {outputs['valid_AUROC']:.4f}")

    def test_step(self, batch, batch_idx):
        loss, outputs = self.step(batch, 'test')

        self.log_dict(outputs, on_step=True, on_epoch=True)

        return loss

    def test_epoch_end(self, outputs: list) -> None:
        epoch_loss = torch.stack(outputs).mean()
        outputs = self.__compute_epoch_metrics('test')

        self.write_to_test_log(f"{epoch_loss:.4f}, {outputs['test_Accuracy']:.4f}, 0\n")
        # self.write_to_test_log(f"{epoch_loss:.4f}, {outputs['test_Accuracy']:.4f}, {outputs['test_AUROC']:.4f}\n")

    def _load_data(self, mode='train'):
        dataset = ProtFunctDatasetMultiClass(
            self.setting.data_address, 
            mode=mode, 
            if_transform=True, 
            dis_cut=self.setting.distance_cutoff,
            use_classes=self.setting.use_classes)

        loader = DataLoader(
            dataset, 
            batch_size=self.setting.batch_size, 
            shuffle=False, 
            collate_fn=collate, 
            num_workers=self.setting.num_workers)

        return loader

    def train_dataloader(self):

        return self._load_data(mode='train')

    def val_dataloader(self):

        return self._load_data(mode='valid')

    def test_dataloader(self):

        return self._load_data(mode='test')

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), self.setting.lr)
        scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(
            optimizer, 
            self.setting.num_epochs, 
            eta_min=1e-4)

        return [optimizer], [scheduler]

    def write_to_step_log(self, context: str):
        with open(self.log_step_file, 'a') as f:
            f.write(f'{context}')

    def write_to_epoch_log(self, context: str):
        with open(self.log_epoch_file, 'a') as f:
            f.write(f'{context}')    

    def write_to_test_log(self, context: str):
        with open(self.log_test_file, 'a') as f:
            f.write(f'{context}')

    def write_to_valid_log(self, context: str):
        with open(self.log_valid_file, 'a') as f:
            f.write(f'{context}')





class ProtBinaryClass(pl.LightningModule):
    def __init__(self, setting: ExpSetting, class_idx: int=0):
        super().__init__()
        self.setting = setting
        self.class_idx = class_idx
        self.__setup_log(setting.log_dir)
        self.__setup_matrices()

        self.model = self.__build_model()
        # print(self.model)

    def __setup_matrices(self):
        metrics = tm.MetricCollection([tm.Accuracy(), tm.AUROC()])
        self.metrics_dict = {}
        self.metrics_dict['train'] = metrics.clone(prefix='train_')
        self.metrics_dict['valid'] = metrics.clone(prefix='valid_')
        self.metrics_dict['test'] = metrics.clone(prefix='test_')

    def __setup_log(self, file_dir):
        # check directory, create if not exist
        if not os.path.exists(file_dir):
            os.makedirs(file_dir)
        
        # log file paths
        self.log_step_file = os.path.join(file_dir, 'step.txt')
        self.log_epoch_file = os.path.join(file_dir, 'epoch.txt')
        self.log_test_file = os.path.join(file_dir, 'test.txt')

        # write head
        self.write_to_step_log(',step_loss\n')
        self.write_to_epoch_log(',loss_valid,acc_valid,auroc_valid,loss_train,acc_train, auroc_train\n')
        
    def __build_model(self):
        model = []

        model.append(SE3TransformerEncoder(self.setting.num_layers, len(residue2idx), self.setting.num_channels, self.setting.num_nlayers, self.setting.num_degrees, edge_dim=3, n_bonds=self.setting.n_bounds, div=self.setting.div, pooling=self.setting.pooling, head=self.setting.head))

        mid_dim = model[0].fibers['out'].n_features

        model.append(nn.Linear(mid_dim, mid_dim))
        model.append(nn.ReLU(inplace=True))
        model.append(nn.Linear(mid_dim, 1))

        return nn.ModuleList(model)

    def forward(self, g):
        """get model prediction"""
        prob = self._run_step(g)

        return prob

    def _run_step(self, g):
        """compute forward"""
        z = g
        for layer in self.model:
            z = layer(z)

        return torch.sigmoid(z)

    def __to_onehot(self, y_list):
        # convert class number to onehot representation

        return F.one_hot(y_list, num_classes=self.setting.num_class)

    def compute_loss(self, preds):
        n = self.setting.batch_size
        if preds.shape[0] < 2*n:
            n = int(preds.shape[0]/2)
        
        pos_loss = -torch.log(preds[:n] + EPS).mean()
        neg_loss = -torch.log(1 - preds[n:] + EPS).mean()
        loss = pos_loss + neg_loss
        
        return loss*1e3

    def __compute_epoch_metrics(self, mode):
        outputs = self.metrics_dict[mode].compute()
        self.metrics_dict[mode].reset()

        return outputs

    def step(self, batch, mode='train'):
        g, targets, pdb = batch
        preds = self._run_step(g).flatten()

        loss = self.compute_loss(preds)

        outputs = self.metrics_dict[mode](preds, targets)
        outputs[f'{mode}_loss'] = loss
        
        return loss, outputs 

    def training_step(self, batch, batch_idx):
        loss, outputs = self.step(batch, 'train')
        
        self.log_dict(outputs, on_step=True, on_epoch=True)
        self.write_to_step_log(f',{loss:.4f}\n')

        return loss

    def training_epoch_end(self, outputs: list) -> None:
        # print(outputs)
        # compute loss on all batches
        epoch_loss = torch.stack([x["loss"] for x in outputs]).mean()
        outputs = self.__compute_epoch_metrics('train')

        self.write_to_epoch_log(f",{epoch_loss:.4f}, {outputs['train_Accuracy']:.4f}, {outputs['train_AUROC']:.4f}\n")

    def validation_step(self, batch, batch_idx):
        loss, outputs = self.step(batch, 'valid')
        outputs['valid_loss'] = loss

        self.log_dict(outputs, on_step=True, on_epoch=True)
        
        return loss
    
    def validation_epoch_end(self, outputs: list) -> None:
        epoch_loss = torch.stack(outputs).mean()
        outputs = self.__compute_epoch_metrics('valid')

        self.write_to_epoch_log(f",{epoch_loss:.4f}, {outputs['valid_Accuracy']:.4f}, {outputs['valid_AUROC']:.4f}")

    def test_step(self, batch, batch_idx):
        loss, outputs = self.step(batch, 'test')

        self.log_dict(outputs, on_step=True, on_epoch=True)
        
        return loss
    
    def test_epoch_end(self, outputs: list) -> None:
        epoch_loss = torch.stack(outputs).mean()
        outputs = self.__compute_epoch_metrics('test')

        self.write_to_test_log(f"{epoch_loss:.4f}, {outputs['test_Accuracy']:.4f}, {outputs['test_AUROC']:.4f}\n")

    def _load_data(self, mode='train'):
        dataset = ProtFunctDatasetBinary(
            self.setting.data_address, 
            mode=mode, 
            if_transform=True, 
            dis_cut=self.setting.distance_cutoff,
            class_idx=self.class_idx)

        loader = DataLoader(
            dataset, 
            batch_size=self.setting.batch_size, 
            shuffle=False, 
            collate_fn=collate_ns, 
            num_workers=self.setting.num_workers)

        return loader

    def train_dataloader(self):

        return self._load_data(mode='train')

    def val_dataloader(self):

        return self._load_data(mode='valid')

    def test_dataloader(self):

        return self._load_data(mode='test')

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), self.setting.lr)
        scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(
            optimizer, 
            self.setting.num_epochs, 
            eta_min=1e-4)

        return [optimizer], [scheduler]

    def write_to_step_log(self, context: str):
        with open(self.log_step_file, 'a') as f:
            f.write(f'{context}')

    def write_to_epoch_log(self, context: str):
        with open(self.log_epoch_file, 'a') as f:
            f.write(f'{context}')  

    def write_to_test_log(self, context: str):
        with open(self.log_test_file, 'a') as f:
            f.write(f'{context}')