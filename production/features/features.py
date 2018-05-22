import vigra
import numpy as np

import nifty
import nifty.graph.agglo as nagglo
import nifty.graph.rag as nrag
import nifty.graph.opt.lifted_multicut as nlmc


def make_filtered_lifted_nh(rag, n_labels, uv_ids, lifted_nh):
    # we can't build the full lifted nh, because the extended fragments
    # connect short-cut lifted edges.
    # Hence, we only create the nhood of small - to - snall fragments WITHOUT extended fragments,
    # small - to - extended and extended to extended

    # find the extended nodes
    extended_node_list = np.array(nrag.findZExtendedNodes(rag), dtype='uint32')
    # filter the initial uv ids to exclude extended nodes
    edge_mask = np.in1d(uv_ids, extended_node_list).reshape(uv_ids.shape)
    edge_mask = (edge_mask == 0).all(axis=1)
    filtered_uv_ids = uv_ids[edge_mask]

    # get the corresponding lifted nh
    graph = nifty.undirectedGraph(n_labels)
    graph.insertEdges(filtered_uv_ids)
    lifted_objective = nlmc.liftedMulticutObjective(graph)
    lifted_objective.insertLiftedEdgesBfs(lifted_nh)
    lifted_uv_ids = lifted_objective.liftedUvIds()

    # next, get the full lifted nh and post filter it for
    # small - to - small fragment connections
    graph = nifty.undirectedGraph(n_labels)
    graph.insertEdges(uv_ids)
    lifted_objective = nlmc.liftedMulticutObjective(graph)
    lifted_objective.insertLiftedEdgesBfs(lifted_nh)
    additional_lifted_uv_ids = lifted_objective.liftedUvIds()

    # filter edges that connect to small fragments
    edge_mask = np.in1d(additional_lifted_uv_ids,
                        extended_node_list).reshape(additional_lifted_uv_ids.shape)
    edge_mask = np.sum(edge_mask, axis=1) > 1
    additional_lifted_uv_ids = additional_lifted_uv_ids[edge_mask]
    if additional_lifted_uv_ids.size:
        lifted_uv_ids = np.concatenate([lifted_uv_ids, additional_lifted_uv_ids], axis=0)
    print("Filtered number of lifted edges:", len(lifted_uv_ids))
    return lifted_uv_ids


def region_features(seg, uv_ids, input_):
    # print("Computing region features ...")
    # FIXME for some reason 'Quantiles' are not working
    statistics = ["Mean", "Variance", "Skewness", "Kurtosis",
                  "Minimum", "Maximum", "Count", "RegionRadii"]
    extractor = vigra.analysis.extractRegionFeatures(input_, seg.astype('uint32', copy=False),
                                                     features=statistics)

    node_features = np.concatenate([extractor[stat_name][:, None].astype('float32')
                                    if extractor[stat_name].ndim == 1
                                    else extractor[stat_name].astype('float32')
                                    for stat_name in statistics],
                                   axis=1)
    fU = node_features[uv_ids[:, 0], :]
    fV = node_features[uv_ids[:, 1], :]

    edge_features = np.concatenate([np.minimum(fU, fV),
                                    np.maximum(fU, fV),
                                    np.abs(fU - fV)], axis=1)
    # print("... done")
    return np.nan_to_num(edge_features)


def ucm_features(n_labels, local_uvs, lifted_uvs, local_probs):

    # print("Computing ucm features ...")
    graph = nifty.graph.undirectedGraph(n_labels)
    graph.insertEdges(local_uvs)
    graph.insertEdges(lifted_uvs)

    all_probs = np.concatenate([local_probs, np.zeros(len(lifted_uvs), dtype='float32')],
                               axis=0)

    feat = nagglo.ucmFeatures(graph, all_probs,
                              edgeSizes=None,
                              nodeSizes=None)[len(local_uvs):]
    # print("... done")
    return np.nan_to_num(feat)


# # FIXME this fails with
# # Nifty assertion !edgeIsLifted_[edgeToContract] failed in file ...
# # RuntimeError: internal error
# def ucm_features(n_labels, lifted_objective, local_probs,
#                  size_regularizers=np.arange(0.1, 1., 0.1)):
#     print("Computing ucm features ...")
#     node_sizes = np.zeros(n_labels)
#     edge_sizes = np.zeros(len(local_probs))
#     if isinstance(size_regularizers, np.ndarray):
#         size_regularizers = size_regularizers.tolist()
#     feat = nlmc.liftedUcmFeatures(lifted_objective, local_probs,
#                                   node_sizes, edge_sizes, size_regularizers)
#     print("... done")
#     return np.nan_to_num(feat)


def clustering_features(graph, probs, lifted_uvs):

    # print("Computing clustering features ...")
    edge_sizes = np.ones(graph.numberOfEdges)
    node_sizes = np.ones(graph.numberOfNodes)

    def cluster(threshold):
        policy = nagglo.malaClusterPolicy(graph=graph,
                                          edgeIndicators=probs,
                                          edgeSizes=edge_sizes,
                                          nodeSizes=node_sizes,
                                          threshold=threshold)

        clustering = nifty.graph.agglo.agglomerativeClustering(policy)
        clustering.run()
        node_labels = clustering.result()
        return (node_labels[lifted_uvs[:, 0]] != node_labels[lifted_uvs[:, 1]]).astype('float32')

    thresholds = (.3, .4, .5, .6, .7, .8)
    features = np.concatenate([cluster(thresh)[:, None]
                               for thresh in thresholds], axis=1)
    state_sum = np.sum(features, axis=1)[:, None]
    # print("... done")
    return np.concatenate([features, state_sum], axis=1)
