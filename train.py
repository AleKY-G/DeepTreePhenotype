#Train
import comet_ml
import glob
import geopandas as gpd
import os
import numpy as np
from src import main
from src import data
from src import start_cluster
from src.models import multi_stage
from src import visualize
from src import metrics
import subprocess
import sys
from pytorch_lightning import Trainer
from pytorch_lightning.loggers import CometLogger
from pytorch_lightning.callbacks import LearningRateMonitor
import pandas as pd
from pandas.util import hash_pandas_object

#Get branch name for the comet tag
git_branch=sys.argv[1]
git_commit=sys.argv[2]

#Create datamodule
config = data.read_config("config.yml")
comet_logger = CometLogger(project_name="DeepTreeAttention2", workspace=config["comet_workspace"], auto_output_logging="simple")    

#Generate new data or use previous run
if config["use_data_commit"]:
    config["crop_dir"] = os.path.join(config["data_dir"], config["use_data_commit"])
    client = None    
else:
    crop_dir = os.path.join(config["data_dir"], comet_logger.experiment.get_key())
    os.mkdir(crop_dir)
    client = start_cluster.start(cpus=50, mem_size="4GB")    
    config["crop_dir"] = crop_dir

comet_logger.experiment.log_parameter("git branch",git_branch)
comet_logger.experiment.add_tag(git_branch)
comet_logger.experiment.log_parameter("commit hash",git_commit)
comet_logger.experiment.log_parameters(config)

data_module = data.TreeData(
    csv_file="data/raw/neon_vst_data_2022.csv",
    data_dir=config["crop_dir"],
    config=config,
    client=client,
    metadata=True,
    comet_logger=comet_logger)

data_module.setup()
if client:
    client.close()

comet_logger.experiment.log_parameter("train_hash",hash_pandas_object(data_module.train))
comet_logger.experiment.log_parameter("test_hash",hash_pandas_object(data_module.test))
comet_logger.experiment.log_parameter("num_species",data_module.num_classes)
comet_logger.experiment.log_table("train.csv", data_module.train)
comet_logger.experiment.log_table("test.csv", data_module.test)

if not config["use_data_commit"]:
    comet_logger.experiment.log_table("novel_species.csv", data_module.novel)

m = multi_stage.MultiStage(data_module.train.copy(), data_module.test.copy(), config=data_module.config, crowns=data_module.crowns)

#Save the train df for each level for inspection
for index, train_df in enumerate([m.level_0_train,
          m.level_1_train]):
    comet_logger.experiment.log_table("train_level_{}.csv".format(index), train_df)

#Save the train df for each level for inspection
for index, test_df in enumerate([m.level_0_test,
          m.level_1_test]):
    comet_logger.experiment.log_table("test_level_{}.csv".format(index), test_df)
    
#Create trainer
lr_monitor = LearningRateMonitor(logging_interval='epoch')
trainer = Trainer(
    gpus=data_module.config["gpus"],
    fast_dev_run=data_module.config["fast_dev_run"],
    max_epochs=data_module.config["epochs"],
    accelerator=data_module.config["accelerator"],
    checkpoint_callback=False,
    num_sanity_val_steps=0,
    callbacks=[lr_monitor],
    logger=comet_logger)

trainer.fit(m)

#Save model checkpoint
trainer.save_checkpoint("/blue/ewhite/b.weinstein/DeepTreeAttention/snapshots/{}.pl".format(comet_logger.experiment.id))

# Prediction datasets are indexed by year, but full data is given to each model before ensembling
predict_datasets = []
for level in range(m.levels):
    ds = data.TreeDataset(df=data_module.test.copy(), train=False, config=config)
    predict_datasets.append(ds)
    
predictions = trainer.predict(m, dataloaders=m.predict_dataloader(ds_list=predict_datasets))
results = m.gather_predictions(predictions)
results["individualID"] = results["individual"]
results = results.merge(data_module.crowns, on="individualID")
comet_logger.experiment.log_table("nested_predictions.csv", results)

ensemble_df = m.ensemble(results)
ensemble_df = m.evaluation_scores(
    ensemble_df,
    experiment=comet_logger.experiment
)

#Log prediction
comet_logger.experiment.log_table("ensemble_df.csv", ensemble_df)

#Visualizations
ensemble_df["pred_taxa_top1"] = ensemble_df.ensembleTaxonID
ensemble_df["pred_label_top1"] = ensemble_df.ens_label
rgb_pool = glob.glob(data_module.config["rgb_sensor_pool"], recursive=True)

#Limit to 1 individual for confusion matrix
ensemble_df = ensemble_df.reset_index(drop=True)
ensemble_df = ensemble_df.groupby("individualID").apply(lambda x: x.head(1))
test = data_module.test.groupby("individualID").apply(lambda x: x.head(1)).reset_index(drop=True)
visualize.confusion_matrix(
    comet_experiment=comet_logger.experiment,
    results=ensemble_df,
    species_label_dict=data_module.species_label_dict,
    test_crowns=data_module.crowns,
    test=test,
    test_points=data_module.canopy_points,
    rgb_pool=rgb_pool
)
