from chem_wasserstein.ElM2D_ import ElM2D
import umap
from operator import attrgetter
from crabnet.crabnet_ import CrabNet
from CrabNet.kingcrab import CrabNet
from CrabNet.model import Model
import pandas as pd
from scipy.stats import multivariate_normal
from sklearn.metrics import mean_squared_error
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import RobustScaler, MinMaxScaler
import numpy as np
from hdbscan.hdbscan_ import HDBSCAN
from mat_discover.mat_discover_ import Discover
import assets.plots as plots
import torch
from tqdm import tqdm
import settings

device = torch.device('cpu')

class DiscoAugment(object):
    def __init__(self, 
                 dfs_dict : dict,
                 a_key: str,         # acceptor
                 d_key: str,         # donor
                 scaler = MinMaxScaler,
                 self_augment = 0.1,
                 score: str = 'dens',
                 ):
        
        self.a_key = a_key
        self.d_key = d_key
        
        self.a_df = dfs_dict[a_key]
        self.d_df = dfs_dict[d_key]
        
        if self_augment is not None:
            #we split acceptor in two parts and auto-augment it.
            initial_size = int(len(self.a_df)*self_augment)
            self.a_df = dfs_dict[a_key].iloc[:initial_size].reset_index(drop=True)
            self.d_df = dfs_dict[a_key].iloc[initial_size:].reset_index(drop=True)
        
        self.dfs_dict = dfs_dict
        self.score    = score
        self.scaler   = scaler    
    
    def predictive_model(self,
                         model_type :str ='crabnet',
                         random_state:int = 1234,
                         crabnet_kwargs: dict = {'epochs':40}
                         ):
        
        if model_type=='crabnet':
            
            # training on acceptor (train) then computing scores on donor (val)
            crabnet_model = Model(CrabNet(compute_device=device).to(device),
                                  classification=False,
                                  random_state=random_state,
                                  verbose=crabnet_kwargs['verbose'],
                                  discard_n=crabnet_kwargs['discard_n'])

            crabnet_model = Model(CrabNet(compute_device=device).to(device),
                                  classification=False,
                                  random_state=random_state,
                                  verbose=crabnet_kwargs['verbose'],
                                  discard_n=crabnet_kwargs['discard_n'])
            
            # little validation for crabnet.
            train_df = self.a_df.loc[:,:'target']
            little_val = train_df.sample(frac=0.10, random_state=random_state)
            train_df = train_df.drop(index=little_val.index)
        
            # loading acceptor data
            crabnet_model.load_data(train_df, train=True)
            crabnet_model.load_data(little_val, train=False)
            crabnet_model.fit(epochs = crabnet_kwargs['epochs'])
            
            # predicting donor data
            crabnet_model.load_data(self.d_df.loc[:,:'target'], train=False)
            d_df_true,d_df_pred,_,_ = crabnet_model.predict(crabnet_model.data_loader)
            
            self.d_df_pred = d_df_pred
            self.d_df_true = d_df_true
            
            
    def compute_umap_embs(self, random_state:int = 1234):
        
        d_df_formula = self.d_df["formula"]
        a_df_formula = self.a_df["formula"]
        all_formula = pd.concat((a_df_formula, d_df_formula), axis=0)
        self.n_a, self.n_d = len(a_df_formula), len(d_df_formula)
        self.ntot = self.n_a + self.n_d
        
        mapper = ElM2D(verbose=False)
        mapper.fit(all_formula)
        dm = mapper.dm #distance matrix.
        
        umap_trans = umap.UMAP(
            densmap=True,
            output_dens=True,
            dens_lambda=1.0,
            n_neighbors=10,
            min_dist=0,
            n_components=2,
            metric="precomputed",
            random_state=random_state,
            low_memory=False,
            ).fit(dm)
        
        umap_emb, r_orig_log, r_emb_log = attrgetter("embedding_", "rad_orig_", "rad_emb_")(
            umap_trans)
        
        # plots.plot_umap(umap_emb, self.n_a)
        umap_r_orig = np.exp(r_orig_log)
        
        self.umap_emb = umap_emb
        self.umap_r_orig = umap_r_orig
        
        self.a_ilist = list(range(self.n_a))              # initial acceptor list
        self.d_ilist = list(range(self.n_a, self.ntot))   # initial donor list
                
        
    def compute_density_score(self):
        a_emb    = self.umap_emb[self.a_ilist]
        a_r_orig = self.umap_r_orig[self.a_ilist]
        d_emb    = self.umap_emb[self.d_ilist]
        d_r_orig = self.umap_r_orig[self.d_ilist]
        
        def my_mvn(mu_x, mu_y, r):
            """Calculate multivariate normal at (mu_x, mu_y) with constant radius, r."""
            return multivariate_normal([mu_x, mu_y], [[r, 0], [0, r]])
        
        #we calculate a list of mvns based on each pair of embeddings of our compounds
        mvn_list = list(map(my_mvn, a_emb[:, 0], a_emb[:, 1], a_r_orig))
        pdf_list = [mvn.pdf(d_emb) for mvn in mvn_list]
        d_dens = np.sum(pdf_list, axis=0)
        # log_d_dens = np.log(d_dens)

        proxy = d_dens.ravel().reshape(-1, 1)
        proxy_scaler = self.scaler().fit(-1*proxy) #why -1?
        proxy_scaled = proxy_scaler.transform(-1*proxy)        

        return proxy_scaled
    
    
    def compute_target_score(self):
        pred = self.d_df_pred.ravel().reshape(-1, 1)
        # Scale and weight the cluster data
        pred_scaler = self.scaler().fit(pred)
        pred_scaled = pred_scaler.transform(pred)
        
        return pred_scaled
    
    
    def compute_weighted_score(self, pred_scaled, proxy_scaled, 
                               pred_weight=1.0, proxy_weight=1.0):
        """Calculate weighted discovery score using the predicted target and proxy."""

        pred_weigthed  = pred_weight * pred_scaled
        proxy_weigthed = proxy_weight * proxy_scaled

        # combined cluster data
        comb_data = pred_weigthed + proxy_weigthed
        comb_scaler = self.scaler().fit(comb_data)

        # cluster scores range between 0 and 1
        score = comb_scaler.transform(comb_data).ravel()
        
        return score
    
    
    def compute_score(self, pred_weight=1.0, proxy_weight=1.0):
        # from 
        # [ilists in self memory
        # embeddings in self memory
        # predictions in self memory]    ----- > compute score
        proxy_scaled = self.compute_density_score()
        pred_scaled = self.compute_target_score()
        output = self.compute_weighted_score(pred_scaled, proxy_scaled, 
                                             pred_weight, proxy_weight)
        return output
    
    
    def compute_clusters(self, 
                         hdbscan_kwargs:dict = {'min_samples': 1, 
                                                'cluster_selection_epsilon': 0.63, 
                                                'min_cluster_size': 50}):
        
        # |  fit_predict(self, X, y=None)
        # |      Performs clustering on X and returns cluster labels.
        
        clusterer = HDBSCAN(**hdbscan_kwargs)
        d_cls_labels = clusterer.fit_predict(self.umap_emb[self.n_a:])
        
        return list(d_cls_labels)
        
    
    def new_dataframe(self):
        
        combo = pd.concat([self.a_df, self.d_df], axis=0).reset_index(drop=True)
        combo = combo.iloc[self.a_ilist]
        # print(len(self.a_ilist))
        
        return combo
        
    
    def apply_augmentation(self,
                           model_type:str = 'crabnet',
                           crabnet_kwargs: dict = {'epochs':40},
                           thresh:float = 0.5,
                           n_iter: int = 15,
                           clusters:bool = False,
                           batch_size: int = 1,
                           proxy_weight :float = 1.0,
                           pred_weight :float = 1.0,
                           by_least_novel:bool = False,
                           random_state:int = 1234):
        
        self.predictive_model(model_type=model_type,
                              random_state=random_state,
                              crabnet_kwargs = crabnet_kwargs)
        
        d_df_pred_original = self.d_df_pred
        
        self.compute_umap_embs(random_state=random_state)
        score = self.compute_score(pred_weight, proxy_weight)
        
        # if clusters:
        #     d_cls_labels = self.compute_clusters()
        #     df_source = pd.DataFrame({'score':score, 'labels':d_cls_labels})
        # else:
        df_source = pd.DataFrame({'score':score})
        
        idxs_cum = []
        output = []
        output.append(self.new_dataframe())
        for i, n in enumerate(tqdm(range(n_iter))):
            if n!=0: score = self.compute_score(pred_weight, proxy_weight)
            
            # if clusters:
            #     for c in list(df_source['labels'].unique()):
            #         # if c!=-1:
            #         mask_c     = df_source['labels'] == c
            #         mask_thr   = df_source['score'] >= thresh
            #         # mask_thr   = df_source['score'] <= thresh
            #         temp = df_source[(mask_c)&(mask_thr)].sort_values(by=['score'],ascending=False)
            #         # temp = df_source[(mask_c)&(mask_thr)].sort_values(by=['score'],ascending=True)
            #         idxs       = list(temp.iloc[:batch_size].index)
            #         idxs_combo = [i+self.n_a for i in idxs]
            #         idxs_cum = idxs_cum+idxs
                    
            #         if idxs:
            #             # augment destination indices 
            #             self.a_ilist = self.a_ilist + idxs_combo
            #             # reduce source indices
            #             self.d_ilist   = [i for i in self.d_ilist if i not in idxs_combo]
            #             # to not recompute target predictions
            #             self.d_df_pred = np.delete(d_df_pred_original, idxs_cum, axis=0)
            #             df_source = df_source.drop(idxs, axis=0)
                        
            #         else:
            #             return output  
                    
            #         if len(self.d_ilist) == 0:
            #             output.append(self.new_dataframe())
                        
            #             return output
                    
            #     output.append(self.new_dataframe())
            
            # else:
            mask_thr   = df_source['score'] >= thresh
            # mask_thr   = df_source['score'] <= thresh
            temp = df_source[(mask_thr)].sort_values(by=['score'],ascending=by_least_novel)
            # temp = df_source[(mask_c)&(mask_thr)].sort_values(by=['score'],ascending=True)
            idxs       = list(temp.iloc[:batch_size].index) #(?)
            idxs_combo = [i+self.n_a for i in idxs]
            idxs_cum = idxs_cum+idxs
            
            if idxs:
                # augment destination indices 
                self.a_ilist = self.a_ilist + idxs_combo
                # reduce source indices
                self.d_ilist   = [i for i in self.d_ilist if i not in idxs_combo]
                # to not recompute target predictions
                self.d_df_pred = np.delete(d_df_pred_original, idxs_cum, axis=0)
                df_source = df_source.drop(idxs, axis=0)
                
            else:
                return output
            
            if len(self.d_ilist) == 0:
                output.append(self.new_dataframe())
                return output
            
        output.append(self.new_dataframe())
            
        return output
    