#!/usr/bin/env python

"""
    models.py
"""

from __future__ import division
from __future__ import print_function

from functools import partial

import torch
from torch import nn
from torch.nn import functional as F

from lr import LRSchedule
from nn_modules import GenerateQuantumWalkGraphs, QuantumWalk

# --
# Model

class GSSupervised(nn.Module):
    def __init__(self,
        input_dim,
        n_nodes,
        n_classes,
        layer_specs, 
        aggregator_class, 
        prep_class, 
        sampler_class, adj, train_adj,
        lr_init=0.01,
        weight_decay=0.0,
        lr_schedule='constant',
        quantum_walk=False,
        epochs=10):
        
        super(GSSupervised, self).__init__()
        
        # --
        # Define network
        
        # Sampler
        self.train_sampler = sampler_class(adj=train_adj)
        self.val_sampler = sampler_class(adj=adj)
        self.train_sample_fns = [partial(self.train_sampler, n_samples=s['n_train_samples']) for s in layer_specs]
        self.val_sample_fns = [partial(self.val_sampler, n_samples=s['n_val_samples']) for s in layer_specs]

        # Make graphs if using the quantum walk aggregator
        #if aggregator_class == "QWAggregator":
        #    self.train_graph_sample_fns = 
        #    self.val_graph_sample_fns = 

        # Prep
        self.prep = prep_class(input_dim=input_dim, n_nodes=n_nodes)
        input_dim = self.prep.output_dim

        #self.aggregator_class = aggregator_class
        self.quantum_walk = quantum_walk
        self.quantum_neighbors = False
        if self.quantum_walk:
            self.quantum_neighbors = True
            self.adj = adj
            self.train_adj = train_adj
            self.time_steps = 4
            self.walk_layer = QuantumWalk()
            self.all_amps = []
            self.all_graphs = []
            self.max_degrees = []
        
        # Network
        agg_layers = []
        for spec in layer_specs:
            agg = aggregator_class(
                input_dim=input_dim,
                output_dim=spec['output_dim'],
                activation=spec['activation'],
            )
            agg_layers.append(agg)
            input_dim = agg.output_dim # May not be the same as spec['output_dim']
        
        self.agg_layers = nn.Sequential(*agg_layers)
        self.fc = nn.Linear(input_dim, n_classes, bias=True)
        
        # --
        # Define optimizer
        
        self.lr_scheduler = partial(getattr(LRSchedule, lr_schedule), lr_init=lr_init)
        self.lr = self.lr_scheduler(0.0)
        self.optimizer = torch.optim.Adam(self.parameters(), lr=self.lr, weight_decay=weight_decay)
    
    def forward(self, ids, feats, train=True):
        # Sample neighbors
        sample_fns = self.train_sample_fns if train else self.val_sample_fns
        if self.quantum_walk:
            adj = self.train_adj if train else self.adj

        has_feats = feats is not None
        tmp_feats = feats[ids] if has_feats else None
        all_feats = [self.prep(ids, tmp_feats, layer_idx=0)]

        original_id_len = len(ids)
        for layer_idx, sampler_fn in enumerate(sample_fns):
            ids = sampler_fn(ids=ids).contiguous().view(-1)
            if self.quantum_neighbors:
                amps, graphs, max_degree = GenerateQuantumWalkGraphs(adj, ids, int(original_id_len), int(len(ids)/original_id_len))
                self.all_amps.append(amps)
                self.all_graphs.append(graphs)
                self.max_degrees.append(max_degree)
            
            tmp_feats = feats[ids] if has_feats else None
            all_feats.append(self.prep(ids, tmp_feats, layer_idx=layer_idx + 1))
        
        if self.quantum_walk:
            self.quantum_neighbors = False
        # Sequentially apply layers, per original (little weird, IMO)
        # Each iteration reduces length of array by one
        for agg_layer in self.agg_layers.children():
            # the quantum walk layer returns the modified neighbors
            if self.quantum_walk:
                all_feats = [agg_layer(all_feats[k], self.walk_layer(all_feats[k], all_feats[k + 1], self.all_amps[k], self.all_graphs[k], self.time_steps, self.max_degrees[k])) for k in range(len(all_feats) - 1)]
            else:
                all_feats = [agg_layer(all_feats[k], all_feats[k + 1]) for k in range(len(all_feats) - 1)]
        assert len(all_feats) == 1, "len(all_feats) != 1"
        out = F.normalize(all_feats[0], dim=1) # ?? Do we actually want this? ... Sometimes ...
        return self.fc(out)
    
    def set_progress(self, progress):
        self.lr = self.lr_scheduler(progress)
        LRSchedule.set_lr(self.optimizer, self.lr)
    
    def train_step(self, ids, feats, targets, loss_fn):
        self.optimizer.zero_grad()
        preds = self(ids, feats, train=True)
        loss = loss_fn(preds, targets.squeeze())
        loss.backward()
        torch.nn.utils.clip_grad_norm(self.parameters(), 5)
        self.optimizer.step()
        return preds

