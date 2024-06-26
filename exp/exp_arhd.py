import warnings
from collections import defaultdict
from typing import Optional, Tuple

import numpy as np
import torch
import torch.nn as nn
from torch import optim
from tqdm import tqdm

from data.data_loader import Dataset_Custom, Dataset_ETT_hour, Dataset_ETT_minute, Dataset_ohio
from exp.exp_basic import Exp_Basic
from models import MultivariateARModel
from utils.metrics import cumavg, metric

warnings.filterwarnings("ignore")


class net(nn.Module):
    def __init__(self, args, device):
        super().__init__()
        self.device = device
        self.model = MultivariateARModel(
            T=args.seq_len, D=args.hvs_len, tau=args.pred_len
        ).to(self.device)

    def forward(self, x):
        return self.model(x)


class ExpARHD(Exp_Basic):
    def __init__(self, args):
        super(ExpARHD, self).__init__(args)
        self.args = args
        self.model = net(args, device=self.device)

    def _get_data(self, flag):
        args = self.args
        print("ExpARHD parameters: \n",args)
        data_dict_ = {
            "ohio540": Dataset_ohio,
            "ETTh1": Dataset_ETT_hour,
            "ETTh2": Dataset_ETT_hour,
            "ETTm1": Dataset_ETT_minute,
            "ETTm2": Dataset_ETT_minute,
            "WTH": Dataset_Custom,
            "ECL": Dataset_Custom,
            "ILI": Dataset_Custom,
            "S-A": Dataset_Custom,
            "custom": Dataset_Custom,
        }
        
        data_dict = defaultdict(lambda: Dataset_Custom, data_dict_)
        Data = data_dict[self.args.data]
        
        timeenc = 1 #for ohio

        freq = args.freq

        data_set = Data(
            root_path=args.root_path,
            data_path=args.data_path,
            flag=flag,
            size=[args.seq_len, args.label_len, args.pred_len], 
            #size = [96,24,24]
            features=args.features,
            target=args.target,
            inverse=False,
            timeenc=timeenc,
            freq=freq,
            cols=args.cols,
        )
        return data_set

    def _select_optimizer(self):
        self.opt = optim.AdamW(self.model.parameters(), lr=self.args.learning_rate)
        return self.opt

    def _select_criterion(self):
        return nn.HuberLoss()

    def train(self):
        tau, Ts = self.args.pred_len, self.args.seq_len  #tau = 6 for ohio
        train_data: np.ndarray = self._get_data(flag="train").data_x
        self._select_optimizer()
        for i in tqdm(range(Ts, train_data.shape[0] - tau, 1)):
            self._process_one_batch(train_data, i, mode="train")

    def test(self):
        test_data: np.ndarray = self._get_data(flag="test").data_x
        preds = []
        trues = []
        rses, corrs = [], []
        for i in tqdm(
            range(
                self.args.seq_len,
                test_data.shape[0] - self.args.pred_len,
                self.args.pred_len,
            )
        ):  
            pred, true = self._process_one_batch(test_data, i, mode="test")
            preds.append(pred.detach().cpu())
            trues.append(true.detach().cpu())
            rse, corr = metric(pred.detach().cpu().numpy(), true.detach().cpu().numpy())
            rses.append(rse)
            corrs.append(corr)

        preds = torch.cat(preds, dim=0).numpy()
        trues = torch.cat(trues, dim=0).numpy()
        print("test shape:", preds.shape, trues.shape)

        RSE, CORR = cumavg(rses), cumavg(corrs)
        rse, corr = RSE[-1], CORR[-1]

        # mae, mse, rmse, mape, mspe = metric(preds, trues)
        print(f"rse:{rse}, corr:{corr}")
        return [rse, corr], RSE, CORR, preds, trues



    # train / test happens here 
    def _process_one_batch(
        self, data: np.ndarray, idx: int, mode: str
    ) -> Optional[Tuple[torch.Tensor, torch.Tensor]]:
        
        if mode == "train":
            x_seq = torch.Tensor(data[idx - self.args.seq_len : idx, :]).to(self.device)
            
            
            for j in range(self.args.pred_len):
                y = torch.Tensor(data[idx + j, :]).to(self.device)
                y_tilda = self.model(x_seq).T
                x_seq = torch.cat((x_seq, y_tilda.detach()))[1:, :]
                loss = self._select_criterion()(y_tilda.view(-1), y)

                # Add regularization :
                l2_reg = torch.tensor(0.0).to(self.device)
                for param in self.model.parameters():
                    l2_reg += torch.norm(param) ** 2
                    
                # L2 regularization to the loss
                loss += self.args.l2_lambda * l2_reg

                self.opt.zero_grad()
                loss.backward()
                self.opt.step()


        elif mode == "test":
            x_seq = torch.Tensor(data[idx - self.args.seq_len : idx, :]).to(self.device)
            # Prediction
            Y_true = torch.zeros((self.args.pred_len, data.shape[1]))
            Y_pred = torch.zeros((self.args.pred_len, data.shape[1]))
            for j in range(self.args.pred_len):
                y = torch.Tensor(data[idx + j, :]).to(self.device)
                y_tilda = self.model(x_seq).T
                x_seq = torch.cat((x_seq, y_tilda.detach()))[1:, :]
                Y_true[j] = y.detach()
                Y_pred[j] = y_tilda.detach()
            
            # Update
            x_seq = torch.Tensor(data[idx - self.args.seq_len : idx, :]).to(self.device)
            for j in range(self.args.pred_len):
                y = torch.Tensor(data[idx + j, :]).to(self.device)
                y_tilda = self.model(x_seq).T
                
                x_seq = torch.cat((x_seq, y_tilda.detach()))[1:, :]
                loss = self._select_criterion()(y_tilda.view(-1), y)

                # Add regularization :
                l2_reg = torch.tensor(0.0).to(self.device)
                
                for param in self.model.parameters():
                    l2_reg += torch.norm(param) ** 2
                # L2 regularization to the loss
                loss += self.args.l2_lambda * l2_reg

                self.opt.zero_grad()
                loss.backward()
                self.opt.step()
            return Y_pred, Y_true
        else:
            raise Exception("mode should belong to ['train', 'test']")
