'''Storage used in /Users/elior/.cache/'''
import logging
import time
import pandas as pd
import numpy as np
from PIL import Image
import copy
import os

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
import torchvision.transforms as transforms
from torchvision.models.feature_extraction import create_feature_extractor
from sklearn.neighbors import KNeighborsClassifier as KNN

from sot_torchvision_models import resnet18, resnet50
from sot_modif_resnet import modify_resnet_model
from data import load_geirhos_transfer_pre, load_data
from geirhos.probabilities_to_decision import ImageNetProbabilitiesTo16ClassesMapping
from models import CNN, transformer
from loss import info_nce_loss
from utils import create_logger, visualize, norm_calc, find_overlap

from models import *

def pretext_train(epoch, pre_type='supervised',  train_loader=DataLoader, model=nn.Module, pre_lr=0.001, 
                  log_interval=100, save_models=False, save_path=None, verbose=False, log=True, logger=None):
    """
    Modify loss and optimizer inside function because that's not something we need to modify easily. Not project focus.
    """
    
    if pre_type=='supervised':
        criterion = nn.CrossEntropyLoss()
        optimizer = optim.Adam(model.parameters(), lr=pre_lr)
        train_correct = 0
        
        model.train()
        for i, data in enumerate(train_loader):
            inputs, labels = data
            optimizer.zero_grad()
            out = model(inputs)
            loss = criterion(out, labels)
            loss.backward()
            optimizer.step()

            pred = out.argmax(dim=1, keepdim=True)
            train_correct += pred.eq(labels.view_as(pred)).sum().item()

            if i % log_interval == 0:
                msg = '[Epoch %d] Batch [%d], Loss: %.3f' % (epoch + 1, i + 1, loss.item())
                if log: logger.info(time.strftime('%Y-%m-%d-%H-%M') + ' - ' + msg)
                if verbose: print(msg)
        
        train_acc = 100. * train_correct / len(train_loader.dataset)

    elif pre_type=='contrastive':
        """ TODO:
        Choose loss: InfoNCE, NT-Xent, Contrastive softmax
        Choose optimizer: SimCLR use LARS
        """
        optimizer = optim.Adam(model.parameters(), lr=pre_lr)
        train_acc = 0
        model.train()
        for i, ((im_x, im_y), _) in enumerate(train_loader):
            optim.zero_grad()
            inputs = torch.cat([im_x, im_y], dim=0)
            out = model(inputs)
            # need to separate again with .chunk(2) ?

            # Apply InfoNCE loss
            loss, acc = info_nce_loss(out=out, temperature=0.5)
            loss.backward()
            optim.step()
            train_acc += acc.item()
            
            # im1, im2 = transforms.ToPILImage()(torch.squeeze(im_x[0])), transforms.ToPILImage()(torch.squeeze(im_y[0]))
            # im1.show()
            # im2.show()

            if i % log_interval == 0:
                msg = '[Epoch %d] Batch [%d], Loss: %.3f' % (epoch + 1, i + 1, loss.item())
                if log: logger.info(time.strftime('%Y-%m-%d-%H-%M') + ' - ' + msg)
                if verbose: print(msg)
        
        # TODO: Is this correct? is the acc computed in InfoNCE logical?
        train_acc /= len(train_loader.dataset)
        train_acc *= 100

    if save_models and save_path is not None: 
        torch.save(model.state_dict(), save_path)
        msg = 'Model saved to {}'.format(save_path)
        if verbose: print(msg)
        if log: logger.info(time.strftime('%Y-%m-%d-%H-%M') + ' - ' + msg)
    msg = '[Epoch %d] Pre-training complete, Acc: %.3f%%' % (epoch + 1, train_acc)
    if log: logger.info(time.strftime('%Y-%m-%d-%H-%M') + ' - ' + msg)
    if verbose: print(msg)

    return model
        
def test(epoch, pre_type='supervised', model=nn.Module, is_vit=False, classifier=nn.Module, test_loader=DataLoader, stage='Pre',
         save_models=False, save_path=None, verbose=False, log=True, logger=None):
    # if pretext objective was contrastive, modify model to adapt to downstream objective
    
    if pre_type == 'contrastive' and stage == 'Pre':
        # Remain in contrastive framework
        test_acc = 0
        test_loss = 0
        model.eval()
        with torch.no_grad():
            for i, ((im_x, im_y), _) in enumerate(test_loader):
                inputs = torch.cat([im_x, im_y], dim=0)
                out = model(inputs)
                loss, acc = info_nce_loss(out=out, temperature=0.5)
                test_acc += acc.item()
                test_loss += loss.item()
            
        # TODO: Is this correct? is the acc computed in InfoNCE logical?
        test_acc /= len(test_loader.dataset)
        test_acc *= 100
        test_loss /= len(test_loader.dataset)

        if save_models and save_path is not None: 
            torch.save(model.state_dict(), save_path)
            msg = 'Model saved to {}'.format(save_path)
            if verbose: print(msg)
            if log: logger.info(time.strftime('%Y-%m-%d-%H-%M') + ' - ' + msg)

        msg = '[Epoch %d] %s-testing complete, Avg Loss: %.3f, Acc: %.3f%%' % (epoch + 1, stage, test_loss, test_acc)
        if log: logger.info(time.strftime('%Y-%m-%d-%H-%M') + ' - ' + msg)
        if verbose: print(msg)
      
    else:
        criterion = nn.CrossEntropyLoss()
        test_correct = 0
        test_loss = 0

        # If downstream testing, the nb_classes should be different from the pre_training, 
        # regardless of pre_type and finetune
        if stage == 'Down':
            if not is_vit: model.fc = nn.Identity()
            else: model.head = nn.Identity()
            classifier.eval()
        model.eval()
        with torch.no_grad():
            for inputs, labels in test_loader:
                out = model(inputs) if stage == 'Pre' else classifier(model(inputs))
                test_loss += criterion(out, labels)
                pred = out.argmax(dim=1, keepdim=True)
                test_correct += pred.eq(labels.view_as(pred)).sum().item() 
                break
        test_acc = 100. * test_correct / len(test_loader.dataset)
        test_loss /= len(test_loader.dataset)

        if save_models and save_path is not None: 
            torch.save(model.state_dict(), save_path)
            msg = 'Model saved to {}'.format(save_path)
            if verbose: print(msg)
            if log: logger.info(time.strftime('%Y-%m-%d-%H-%M') + ' - ' + msg)

        msg = '[Epoch %d] %s-testing complete, Avg Loss: %.3f, Acc: %.3f%%' % (epoch + 1, stage, test_loss, test_acc)
        if log: logger.info(time.strftime('%Y-%m-%d-%H-%M') + ' - ' + msg)
        if verbose: print(msg)

    return test_acc

def down_finetune(epoch, finetune_epochs=5, pre_type='supervised', train_loader=DataLoader, 
                  model=nn.Module, is_vit=False, classifier=nn.Module, down_lr=0.001,
                  log_interval=100, save_models=False, save_path=None, verbose=False, log=True, logger=None):
    if pre_type=='supervised':
        # modify fc dimensions and finetune with standard training procedure
        if not is_vit: model.fc = classifier
        else: model.head = classifier
        model.train()
    
    elif pre_type=='contrastive':
        # freeze encoder and train a head
        if not is_vit: model.fc = nn.Identity()
        else: model.head = nn.Identity()
        model.eval()
        classifier.train()
    
    criterion = nn.CrossEntropyLoss()
    optimizer = optim.Adam(model.parameters(), lr=down_lr)
    for e in range(finetune_epochs):
        train_correct = 0
        train_loss = 0
        for i, data in enumerate(train_loader):
            inputs, labels = data
            optimizer.zero_grad()
            
            if pre_type=='supervised':
                out = model(inputs)
            elif pre_type=='contrastive':
                out = classifier(model(inputs))
            
            loss = criterion(out, labels)
            loss.backward()
            optimizer.step()

            pred = out.argmax(dim=1, keepdim=True)
            train_correct += pred.eq(labels.view_as(pred)).sum().item()
            train_loss += loss
            break

            if i % log_interval == 0:
                msg = '[Finetuning Epoch %d] Batch [%d], Loss: %.3f' % (e + 1, i + 1, loss.item())
                if log: logger.info(time.strftime('%Y-%m-%d-%H-%M') + ' - ' + msg)
                if verbose: print(msg)

    train_acc = 100. * train_correct / len(train_loader.dataset)
    train_loss /= len(train_loader.dataset)

    if save_models and save_path is not None: 
        torch.save(model.state_dict(), save_path)
        msg = 'Model saved to {}'.format(save_path)
        if verbose: print(msg)
        if log: logger.info(time.strftime('%Y-%m-%d-%H-%M') + ' - ' + msg)
    
    msg = '[Epoch %d] finetuning complete, Avg Loss: %.3f, Acc: %.3f%%' % (epoch + 1, train_loss, train_acc)
    if log: logger.info(time.strftime('%Y-%m-%d-%H-%M') + ' - ' + msg)
    if verbose: print(msg)

    return model

def evaluate_bias(model, data_path, mapping, 
                  log=True, verbose=False, logger=None, epoch=0):
    #TODO: normalize the images? if yes: compute mean,std once and add line transforms.Normalize((XX, XX, XX), (XX, XX, XX))
    model.eval()
    results = []
    test_size = len(data_path)
    with torch.no_grad():
        for path in data_path:
            # print(path.split("/")[-1])
            img = Image.open(path)
            
            shape, texture = path.split("/")[-1].split("-")[0], path.split("/")[-1].split("-")[1] # might not work on other OS where / is \
            shape = shape[:-2] if shape[-2].isdigit() else shape[:-1]
            texture = texture[:-6] if texture[-6].isdigit() else texture[:-5]
            # img.show()
            
            img_tensor = transforms.ToTensor()(img)
            img_tensor = torch.unsqueeze(img_tensor, dim=0)
            out = model(img_tensor)
            pred = out.argmax(dim=1, keepdim=True)

            # print("1000 classes pred: ", dict[pred.item()], out.max(), out.min())
            
            softmax = torch.nn.Softmax(dim=1)(out)
            out_squeeze = torch.squeeze(softmax)

            out_np = out_squeeze.numpy()
            map = mapping.probabilities_to_decision(out_np)

            # print("16 classes pred: ", map)
            results.append(np.array([map, shape, texture]))

    results = pd.DataFrame(results, columns=["Map", "Shape", "Texture"])
    correct_shape = results.loc[results['Map'] == results['Shape']]
    correct_texture = results.loc[results['Map'] == results['Texture']]
    
    nb_shape = len(correct_shape.index)
    nb_texture = len(correct_texture.index)
    shape_bias = nb_shape / (nb_shape + nb_texture)

    overlap = find_overlap(correct_shape.index, correct_texture.index)
    accuracy = (nb_shape + nb_texture - overlap) / test_size
    
    msg = '[Epoch %d] Standard Bias Eval complete, Shape Bias: %.3f%% Acc: %.3f%%' % (epoch + 1, shape_bias, accuracy)
    if log: logger.info(time.strftime('%Y-%m-%d-%H-%M') + ' - ' + msg)
    if verbose: print(msg)

    return shape_bias, accuracy

def evaluate_bias_embed(model, data_path, nb_neigh=5, is_vit=False,
                        log=True, verbose=False, logger=None, epoch=0):
    embed_map = {
        "knife"    : 0, 
        "keyboard" : 1, 
        "elephant" : 2, 
        "bicycle"  : 3, 
        "airplane" : 4,
        "clock"    : 5, 
        "oven"     : 6, 
        "chair"    : 7, 
        "bear"     : 8, 
        "boat"     : 9, 
        "cat"      : 10, 
        "bottle"   : 11,
        "truck"    : 12, 
        "car"      : 13,
        "bird"     : 14, 
        "dog"      : 15
    }
    model_results = np.ones(shape=(nb_neigh, 2))
    if not is_vit: model.fc = nn.Identity()
    else: model.head = nn.Identity()
    model.eval()
    
    for it in range(1, nb_neigh+1):
        results = []
        test_size = len(data_path)
        embeddings = []
        gt = np.ones(shape=(test_size, 2))
        with torch.no_grad():
            # collect all embeddings
            for i in range(len(data_path)):
                path = data_path[i]
                img = Image.open(path)
        
                shape, texture = path.split("/")[-1].split("-")[0], path.split("/")[-1].split("-")[1] # might not work on other OS where / is \
                shape = shape[:-2] if shape[-2].isdigit() else shape[:-1]
                texture = texture[:-6] if texture[-6].isdigit() else texture[:-5]
                
                img_tensor = transforms.ToTensor()(img)
                img_tensor = torch.unsqueeze(img_tensor, dim=0)
                embedding = torch.squeeze(model(img_tensor)).numpy()

                embeddings.append(embedding)
                gt[i] = [embed_map[shape], embed_map[texture]]

            # eval with KNN
            for idx in range(len(embeddings)):
                train_embeddings = np.delete(np.asarray(embeddings), idx, axis=0)
                test_embedding = np.expand_dims(embeddings[idx], axis=0)
                train_gt = np.delete(gt, idx, axis=0)
                test_gt = gt[idx]

                # Fit for all but this embedding
                # TODO: which metric to use, using cosine <=> normalizing
                neigh_shape = KNN(n_neighbors=it, metric='cosine')
                neigh_shape.fit(X=train_embeddings, y=train_gt[:, 0])
                neigh_texture = KNN(n_neighbors=it, metric='cosine')
                neigh_texture.fit(X=train_embeddings, y=train_gt[:, 1])

                # Predict on this embedding
                pred_shape = np.squeeze(neigh_shape.predict(test_embedding))
                pred_texture = np.squeeze(neigh_texture.predict(test_embedding))

                results.append(np.array([pred_shape, pred_texture, test_gt[0], test_gt[1]]))

        results = pd.DataFrame(results, columns=["Predicted Shape", "Predicted Texture", "Shape", "Texture"])
        correct_shape = results.loc[results['Predicted Shape'] == results['Shape']]
        correct_texture = results.loc[results['Predicted Texture'] == results['Texture']]
        
        nb_shape = len(correct_shape.index)
        nb_texture = len(correct_texture.index)
        shape_bias = nb_shape / (nb_shape + nb_texture)
        
        overlap = find_overlap(correct_shape.index, correct_texture.index)
        accuracy = (nb_shape + nb_texture - overlap) / test_size

        model_results[it-1, 0], model_results[it-1, 1] = shape_bias, accuracy

    model_bias_avg, model_acc_avg = np.average(model_results, axis=0, keepdims=True)[0]

    msg = '[Epoch %d] Embedding Bias Eval complete, with %d neighbors - Shape Bias: %.3f%% Acc: %.3f%%' % (epoch + 1, nb_neigh, model_bias_avg, model_acc_avg)
    if log: logger.info(time.strftime('%Y-%m-%d-%H-%M') + ' - ' + msg)
    if verbose: print(msg)

    return model_bias_avg, model_acc_avg

def evaluate_embed_dist(epoch, model=nn.Module, is_vit=False, test_loader=DataLoader, 
                        save_models=False, save_path=None, verbose=False, log=True, logger=None):
    # Testing the encoder with embedding distances
    
    if not is_vit: model.fc = nn.Identity()
    else: model.head = nn.Identity()
    
    model.eval()
    avg_dist = 0
    with torch.no_grad():
        for inputs, _ in test_loader:
            nb_views = len(inputs)
            batch_size = len(inputs[0])
            inputs = torch.cat([view for view in inputs], dim=0)
            out = model(inputs)

            div = ((nb_views-1) * ((nb_views-1) + 1)) / 2
            # out_dim = (nb_views * batch_size, 512) -- TODO: 512 depend on model? need to modify?
            for h in out.chunk(batch_size, dim=0):
                mean_dist = norm_calc(tens=h, type='euclidian', div=div)
                avg_dist += mean_dist 
    avg_dist /= len(test_loader.dataset)

    if save_models and save_path is not None: 
        torch.save(model.state_dict(), save_path)
        msg = 'Model saved to {}'.format(save_path)
        if verbose: print(msg)
        if log: logger.info(time.strftime('%Y-%m-%d-%H-%M') + ' - ' + msg)

    msg = '[Epoch %d] Pair embeddings distance testing complete, Avg Distance: %.3f' % (epoch + 1, avg_dist)
    if log: logger.info(time.strftime('%Y-%m-%d-%H-%M') + ' - ' + msg)
    if verbose: print(msg)

    return avg_dist


def main(models2compare=[], train_epochs=10, pre_type='supervised', pre_dataset='CIFAR10', 
            finetune=False, down_dataset='CIFAR10', 
            test_interval=5, save_models=False, experiment_id='test1_elior', modelnames=[]):    
        
        """
        Returns: score_table: shape = (nb of models to compare, nb of logged epochs)
                    score_table indices: name of models
                    score_table columns: logged epochs
                    inside each element: [pretext_test, bias_percentage, downstream_test, embed_pair_dist]
                    access a score: score_table.loc['model name', 'epoch nb'][idx]
        """

        assert len(models2compare) == len(modelnames), 'Provide a list of model names with same length as list of models to be tested'

        pre_train, pre_test = load_data(dataset=pre_dataset, stage='pre', finetune=finetune)
        down_train, down_test = load_data(dataset=down_dataset, stage='down', finetune=finetune)

        bias_data_path = load_geirhos_transfer_pre(conflict_only=True)
        class_name_2_nb_classes = {
            'ImageNet' : 1000,
            'CIFAR10'  : 10,
            'CIFAR100' : 100,
            'STL10'    : 10,
        }

        logger = create_logger(experiment_id)
        logger.info(time.strftime('%Y-%m-%d-%H-%M') + ' - ' + 'Starting {} pre-learning on {}, testing on {}'.format(pre_type, pre_dataset, down_dataset))

        scores = []
        scores_idx = 0
        scores_epochs = []
        for model in models2compare:
            scores.append([])
            load = False
            # for model.fc / model.head
            try: model.fc
            except: is_vit = True
            else: is_vit = False

            for epoch in range(train_epochs):
                # Train on pretext task
                save_path = '{}_{}_pre.pth'.format(modelnames[scores_idx], epoch+1)

                if load: 
                    model.load_state_dict(torch.load('model_test.pth'))
                    load = False
                model = pretext_train(pre_type=pre_type, train_loader=pre_train, model=model, pre_lr=0.001,
                                      log_interval=100, save_models=save_models, save_path=save_path, verbose=False, log=True, logger=logger, epoch=epoch)

                # Test
                if epoch % test_interval == 0:
                    # Pretext test
                    if pre_dataset != 'noise':
                        result_pre = test(pre_type=pre_type, model=model, test_loader=pre_test, is_vit=is_vit, stage='Pre',
                                          save_models=save_models, save_path=None, verbose=False, log=True, logger=logger, epoch=epoch)

                    
                    # Shape bias with Geirhos method (1200 images in his custom set)
                    if pre_dataset == 'Imagenet': # standard classification
                        result_bias, result_acc = evaluate_bias(model=model, data_path=bias_data_path, mapping=ImageNetProbabilitiesTo16ClassesMapping(),
                                                                log=True, verbose=False, logger=logger, epoch=epoch)
                    else: # KNN classification of embeddings
                        torch.save(model.state_dict(), 'model_test.pth')
                        load = True
                        # TODO: Modify function to only select one nb of neighbours
                        result_bias, result_acc = evaluate_bias_embed(model=model, data_path=bias_data_path, nb_neigh=5, is_vit=is_vit,
                                                                        log=True, verbose=False, logger=logger, epoch=epoch)
                        
                        model = model.load_state_dict(torch.load('model_test.pth'))

                    # Downstream
                    out_features = class_name_2_nb_classes[down_dataset]
                    if not is_vit: classifier = nn.Linear(in_features=model.fc.in_features, out_features=out_features)
                    else: classifier = nn.Linear(in_features=model.head.in_features, out_features=out_features)

                    # Finetune on downstream task
                    if finetune:
                        down_finetune(model=model, finetune_epochs=5, pre_type=pre_type, train_loader=down_train, 
                                      down_lr=0.001, classifier=classifier, is_vit=is_vit,
                                      log_interval=100, save_models=False, save_path=None, verbose=False, log=True, logger=logger, epoch=epoch)
                
                    # Test on downstream task
                    result_down = test(pre_type=pre_type, model=model, test_loader=down_test, classifier=classifier, is_vit=is_vit, stage='Down',
                                       save_models=save_models, save_path=None, verbose=False, log=True, logger=logger, epoch=epoch)

                    # Pair Embeddings distance 
                    result_pair_dist = evaluate_embed_dist(model=model, is_vit=is_vit, test_loader=down_test, 
                                                           save_models=save_models, save_path=None, verbose=False, log=True, logger=logger, epoch=epoch)


                    # Save all results
                    if pre_dataset != 'noise':
                        scores[scores_idx].append([result_pre, result_bias, result_down, result_pair_dist])
                    else :
                        scores[scores_idx].append([result_bias, result_down, result_pair_dist])
                    if scores_idx == 0: scores_epochs.append(epoch+1)

                '''scheduler.step()'''

            scores_idx += 1
        
        score_table = pd.DataFrame(scores, index=modelnames, columns=scores_epochs)

        return score_table


if __name__ == '__main__':

    res18 = resnet18(num_classes=10)
    res50 = resnet50(num_classes=10)

    ResNet18_for_CIFAR = modify_resnet_model(res18)
    models_to_compare = [] # = ['ViT contrastive', 'ResNet50 supervised', ...]
    models_to_compare.append(ResNet18_for_CIFAR)
    model_names = ['ResNet18_for_CIFAR']

    scores = main(models2compare=models_to_compare, train_epochs=5, test_interval=2, 
                    save_models=True, experiment_id='test1', modelnames=model_names)
    
    pd.DataFrame(scores).to_excel(os.path.join('scores', 'FULL.xlsx'))
    visualize(scores, model_names, save=True, pre_data='not_noise')