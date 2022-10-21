from src import predict
from src import data
from src import neon_paths
from glob import glob
import geopandas as gpd
import pandas as pd
from src.start_cluster import start
from src.models import multi_stage
from distributed import wait, as_completed
import os
import re
import traceback
from pytorch_lightning.loggers import CometLogger
from pytorch_lightning import Trainer

def find_rgb_files(site, config, year="2021"):
    tiles = glob(config["rgb_sensor_pool"], recursive=True)
    tiles = [x for x in tiles if site in x]
    tiles = [x for x in tiles if "neon-aop-products" not in x]
    tiles = [x for x in tiles if "/{}/".format(year) in x]
    
    #DEBUG only do a test tile
    tiles = [x for x in tiles if "404000_3286000" in x]

    #Only allow tiles that are within OSBS station boundary
    osbs_tiles = []
    for rgb_path in tiles:
        basename = os.path.basename(rgb_path)
        geo_index = re.search("(\d+_\d+)_image", basename).group(1)
        if ((float(geo_index.split("_")[0]) > 399815.5) &
        (float(geo_index.split("_")[0]) < 409113.7) &
        (float(geo_index.split("_")[1]) > 3282308) &
        (float( geo_index.split("_")[1]) < 3290124)):
            osbs_tiles.append(rgb_path)
            
    return osbs_tiles

def convert(rgb_path, hyperspectral_pool, savedir):
    #convert .h5 hyperspec tile if needed
    basename = os.path.basename(rgb_path)
    geo_index = re.search("(\d+_\d+)_image", basename).group(1)
    
    h5_list = [x for x in hyperspectral_pool if geo_index in x]
    tif_paths = []
    for path in h5_list:
        year = path.split("/")[6]
        tif_basename = os.path.splitext(os.path.basename(rgb_path))[0] + "_hyperspectral_{}.tif".format(year)
        tif_path = "{}/{}".format(savedir, tif_basename)
        if not os.path.exists(tif_path):
            tif_paths.append(neon_paths.convert_h5(path, rgb_path, savedir, year=year))
        else:
            tif_paths.append(tif_path)
    
    return tif_paths

#Params
config = data.read_config("config.yml")
config["preload_images"] = False
comet_logger = CometLogger(project_name="DeepTreeAttention2", workspace=config["comet_workspace"], auto_output_logging="simple")    
comet_logger.experiment.add_tag("prediction")
comet_logger.experiment.log_parameters(config)

cpu_client = start(cpus=5, mem_size="10GB")

dead_model_path = "/orange/idtrees-collab/DeepTreeAttention/Dead/snapshots/c4945ae57f4145948531a0059ebd023c.pl"
config["crop_dir"] = "/blue/ewhite/b.weinstein/DeepTreeAttention/67ec871c49cf472c8e1ae70b185addb1"
savedir = config["crop_dir"] 

species_model_paths = ["/blue/ewhite/b.weinstein/DeepTreeAttention/snapshots/ac7b4194811c4bdd9291892bccc4e661.pt"]

#generate HSI_tif data if needed.
h5_pool = glob(config["HSI_sensor_pool"], recursive=True)
h5_pool = [x for x in h5_pool if not "neon-aop-products" in x]
hyperspectral_pool = glob(config["HSI_tif_dir"]+"*")

### Step 1 Find RGB Tiles and convert HSI
tiles = find_rgb_files(site="OSBS", config=config)
tif_futures = cpu_client.map(
    convert,
    tiles,
    hyperspectral_pool=h5_pool,
    savedir=config["HSI_tif_dir"])
wait(tif_futures)

#for x in tiles:
    #basename = os.path.splitext(os.path.basename(x))[0]                
    #shpname = "/blue/ewhite/b.weinstein/DeepTreeAttention/results/crowns/{}.shp".format(basename)      
    #try:
        #crowns = predict.find_crowns(rgb_path=x, config=config, dead_model_path=dead_model_path)   
    #except:
        #continue
    #crowns.to_file(shpname)

crown_annotations_paths = []
crown_annotations_futures = []
for x in tiles:
    basename = os.path.splitext(os.path.basename(x))[0]                
    shpname = "/blue/ewhite/b.weinstein/DeepTreeAttention/results/crowns/{}.shp".format(basename)    
    try:
        crowns = gpd.read_file(shpname)    
    except:
        continue
    if not os.path.exists("/blue/ewhite/b.weinstein/DeepTreeAttention/results/crops/{}.shp".format(basename)):
        future = cpu_client.submit(predict.generate_prediction_crops,crowns, config, as_numpy=True)
        crown_annotations_futures.append(future)
    else:
        crown_annotations_path = "/blue/ewhite/b.weinstein/DeepTreeAttention/results/crops/{}.shp".format(basename)       
        crown_annotations_paths.append(crown_annotations_path)

wait(crown_annotations_futures)
 
for x in crown_annotations_futures:
    try:
        crown_annotations_paths.append(x.result())
    except Exception as e:
        print(e)
        
#Recursive predict to avoid prediction levels that will be later ignored.
trainer = Trainer(gpus=config["gpus"], logger=False, enable_checkpointing=False)

## Step 2 - Predict Crowns
for species_model_path in species_model_paths:
    # Load species model
    m = multi_stage.MultiStage(train_df=train, test_df=test, crowns=None, config = config, train_mode=False)
    m.models.load_state_dict(species_model_path)    
    prediction_dir = os.path.join("/blue/ewhite/b.weinstein/DeepTreeAttention/results/",
                                  os.path.splitext(os.path.basename(species_model_path))[0])    
    try:
        os.mkdir(prediction_dir)
    except:
        pass
    for x in crown_annotations_paths:
        results_shp = os.path.join(prediction_dir, os.path.basename(x))  
        if not os.path.exists(results_shp):        
            print(x)
            predict.predict_tile(
                        crown_annotations=x,
                        filter_dead=True,
                        trainer=trainer,
                        m=m,
                        savedir=prediction_dir,
                        config=config)