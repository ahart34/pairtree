import numpy as np
import scipy.stats
import common
import mutrel
from progressbar import progressbar
import phi_fitter
import hyperparams as hparams
import math
import util

Models = common.Models
debug = common.debug
Mutrel = mutrel.Mutrel

from collections import namedtuple
TreeSample = namedtuple('TreeSample', (
  'adj',
  'anc',
  'depth_frac',
  'phi',
  'llh_phi',
))

def _calc_llh_phi(phi, V, N, omega_v, epsilon=1e-5):
  K, S = phi.shape
  for arr in V, N, omega_v:
    assert arr.shape == (K-1, S)

  assert np.allclose(1, phi[0])
  P = omega_v * phi[1:]
  P = np.maximum(P, epsilon)
  P = np.minimum(P, 1 - epsilon)

  phi_llh = scipy.stats.binom.logpmf(V, N, P)
  phi_llh = np.sum(phi_llh)
  assert not np.isnan(phi_llh)
  assert not np.isinf(phi_llh)
  return phi_llh

def _init_cluster_adj_linear(K):
  cluster_adj = np.eye(K, dtype=np.int)
  for k in range(1, K):
    cluster_adj[k-1,k] = 1
  return cluster_adj

def _init_cluster_adj_branching(K):
  cluster_adj = np.eye(K, dtype=np.int)
  # Every node comes off node 0, which will always be the tree root. Note that
  # we don't assume that the first cluster (node 1, cluster 0) is the clonal
  # cluster -- it's not treated differently from any other nodes/clusters.
  cluster_adj[0,:] = 1
  return cluster_adj

def _init_cluster_adj_random(K):
  # Parents for nodes [1, ..., K-1].
  parents = []
  # Note this isn't truly random, since node i can only choose a parent <i.
  # This prevents cycles.
  for idx in range(1, K):
    parents.append(np.random.randint(0, idx))
  cluster_adj = np.eye(K, dtype=np.int)
  cluster_adj[parents, range(1,K)] = 1
  return cluster_adj

def _init_cluster_adj_mutrels(data_mutrel):
  # Hyperparams:
  #   * theta: weight of `B_A` pairwise probabilities
  #   * kappa: weight of depth_frac

  K = len(data_mutrel.rels) + 1
  adj = np.eye(K, dtype=np.int)
  depth = np.zeros(K, dtype=np.int)
  in_tree = set((0,))
  remaining = set(range(1, K))

  W_nodes      = np.zeros(K)
  W_nodes[1:] += np.sum(data_mutrel.rels[:,:,Models.A_B], axis=1)
  # Root should never be selected.
  assert W_nodes[0] == 0

  while len(remaining) > 0:
    if np.all(W_nodes[list(remaining)] == 0):
      W_nodes[list(remaining)] = 1
    W_nodes_norm = W_nodes / np.sum(W_nodes)
    # nidx: node index
    # cidx: cluster index
    nidx = _sample_cat(W_nodes_norm)
    cidx = nidx - 1
    assert data_mutrel.vids[cidx] == 'S%s' % cidx

    anc_probs = data_mutrel.rels[cidx,:,Models.B_A]
    assert anc_probs[cidx] == 0

    if np.any(depth > 0):
      depth_frac = depth / np.max(depth)
    else:
      # All nodes are at zero depth.
      depth_frac = np.copy(depth)
    assert depth_frac[0] == 0

    W_parents      = np.zeros(K)
    W_parents[0]  += hparams.theta * (1 - np.max(anc_probs))
    W_parents[1:] += hparams.theta * anc_probs
    W_parents     += hparams.kappa * depth_frac
    # `W_anc[nidx]` and `depth_frac[nidx]` should both have been zero.
    assert W_parents[nidx] == 0

    # Don't select a parent not yet in the tree.
    W_parents[list(remaining)] = 0
    W_parents_intree = W_parents[list(in_tree)]
    # If all potential parents have zero weight (e.g., because their ancestral
    # probabilities are zero), establish uniform distribution over them.
    if np.all(W_parents_intree == 0):
      W_parents_intree[:] = 1
    # Normalize before setting minimum.
    W_parents_intree /= np.sum(W_parents_intree)
    W_parents_intree  = np.maximum(1e-5, W_parents[list(in_tree)])
    # Renormalize.
    W_parents_intree /= np.sum(W_parents_intree)
    W_parents[list(in_tree)] = W_parents_intree

    parent = _sample_cat(W_parents)
    adj[parent,nidx] = 1
    depth[nidx] = depth[parent] + 1

    remaining.remove(nidx)
    in_tree.add(nidx)
    W_nodes[nidx] = 0

  assert np.all(W_nodes == 0)
  assert depth[0] == 0 and np.all(depth[1:] > 0)
  return adj

def _modify_tree(adj, anc, A, B):
  '''If `B` is ancestral to `A`, swap nodes `A` and `B`. Otherwise, move
  subtree `B` under `A`.

  `B` can't be 0 (i.e., the root node), as we want always to preserve the
  property that node zero is root.'''
  K = len(adj)
  # Ensure `B` is not zero.
  assert 0 <= A < K and 0 < B < K
  assert A != B

  adj = np.copy(adj)
  anc = np.copy(anc)

  assert np.array_equal(np.diag(adj), np.ones(K))
  # Diagonal should be 1, and every node except one of them should have a parent.
  assert np.sum(adj) == K + (K - 1)
  # Every column should have two 1s in it corresponding to self & parent,
  # except for column denoting root.
  assert np.array_equal(np.sort(np.sum(adj, axis=0)), np.array([1] + (K - 1)*[2]))

  np.fill_diagonal(adj, 0)
  np.fill_diagonal(anc, 0)

  if anc[B,A]:
    adj_BA = adj[B,A]
    assert anc[A,B] == adj[A,B] == 0
    if adj_BA:
      adj[B,A] = 0

    # Swap position in tree of A and B. I need to modify both the A and B
    # columns.
    acol, bcol = np.copy(adj[:,A]), np.copy(adj[:,B])
    arow, brow = np.copy(adj[A,:]), np.copy(adj[B,:])
    adj[A,:], adj[B,:] = brow, arow
    adj[:,A], adj[:,B] = bcol, acol

    if adj_BA:
      adj[A,B] = 1
    #debug('tree_permute', (A,B), 'swapping', A, B)
  else:
    # Move B so it becomes child of A. I don't need to modify the A column.
    adj[:,B] = 0
    adj[A,B] = 1
    #debug('tree_permute', (A,B), 'moving', B, 'under', A)

  np.fill_diagonal(adj, 1)
  return adj

def _calc_depth_frac(adj):
  K = len(adj)
  root = 0
  Z = np.copy(adj)
  np.fill_diagonal(Z, 0)

  depth = np.zeros(K)
  stack = [root]
  while len(stack) > 0:
    P = stack.pop()
    C = np.flatnonzero(Z[P])
    if len(C) == 0:
      continue
    depth[C] = depth[P] + 1
    stack += list(C)

  assert depth[root] == 0
  assert np.all(depth[:root] > 0) and np.all(depth[root+1:] > 0)
  depth_frac = depth / np.max(depth)
  return depth_frac

def calc_binom_params(supervars):
  svids = common.extract_vids(supervars)
  V = np.array([supervars[svid]['var_reads'] for svid in svids])
  R = np.array([supervars[svid]['ref_reads'] for svid in svids])
  omega_v = np.array([supervars[svid]['omega_v'] for svid in svids])
  assert np.all(omega_v == 0.5)

  N = V + R
  return (V, N, omega_v)

def _find_parents(adj):
  adj = np.copy(adj)
  np.fill_diagonal(adj, 0)
  return np.argmax(adj[:,1:], axis=0)

def _find_parent(node, adj):
  col = np.copy(adj[:,node])
  col[node] = 0
  parents = np.flatnonzero(col)
  assert len(parents) == 1
  return parents[0]

def _make_W_nodes_mutrel(adj, data_logmutrel):
  K = len(adj)
  assert adj.shape == (K, K)

  tree_logmutrel = _calc_tree_logmutrel(adj, data_logmutrel)
  pair_error = 1 - np.exp(tree_logmutrel)
  assert np.allclose(0, np.diag(pair_error))
  assert np.allclose(0, pair_error[0])
  assert np.allclose(0, pair_error[:,0])
  pair_error = np.maximum(1e-10, pair_error)
  node_error = scipy.special.logsumexp(np.log(pair_error), axis=1)

  weights = np.zeros(K)
  weights[1:] += util.softmax(node_error[1:])
  weights[1:] = np.maximum(1e-10, weights[1:])
  weights /= np.sum(weights)
  assert weights[0] == 0 and np.all(weights[1:] > 0)

  return weights

def _make_W_nodes_uniform(adj):
  K = len(adj)
  weights = np.ones(K)
  weights[0] = 0
  weights /= np.sum(weights)
  return weights

def _make_data_logmutrel(mutrel):
  K = len(mutrel.rels)
  valid_models = (Models.A_B, Models.B_A, Models.diff_branches)
  invalid_models = (Models.cocluster, Models.garbage)

  alpha = 0.001
  logrels = np.full(mutrel.rels.shape, np.nan)
  logrels[:,:,invalid_models] = -np.inf
  logrels[:,:,valid_models] = np.log(mutrel.rels[:,:,valid_models] + alpha)

  logrels[range(K),range(K),:] = -np.inf
  logrels[range(K),range(K),Models.cocluster] = 0

  logrels[:,:,valid_models] -= np.log(1 + len(valid_models)*alpha)
  assert np.allclose(0, scipy.special.logsumexp(logrels, axis=2))
  assert not np.any(np.isnan(logrels))

  logmutrel = Mutrel(rels=logrels, vids=mutrel.vids)
  return logmutrel

def _determine_node_rels(adj):
  adj = np.copy(adj)
  assert np.all(np.diag(adj) == 1)
  np.fill_diagonal(adj, 0)

  K = len(adj)
  node_rels = np.full((K, K), Models.diff_branches)
  stack = [0]
  visited = set()

  np.fill_diagonal(node_rels, Models.cocluster)
  node_rels[0,1:] = Models.A_B

  while len(stack) > 0:
    P = stack.pop()
    visited.add(P)
    C = list(np.flatnonzero(adj[P]))
    if len(C) == 0:
      continue

    P_anc = list(np.flatnonzero(node_rels[P] == Models.B_A))
    C_anc = P_anc + [P]
    node_rels[np.ix_(C_anc,C)] = Models.A_B
    node_rels[np.ix_(C,C_anc)] = Models.B_A

    stack += C

  assert visited == set(range(K))
  assert np.all(np.diag(node_rels) == Models.cocluster)
  assert np.all(node_rels[0,1:] == Models.A_B)
  assert np.all(node_rels[1:,0] == Models.B_A)

  return node_rels

def _calc_tree_logmutrel(adj, data_logmutrel):
  node_rels = _determine_node_rels(adj)
  K = len(node_rels)
  assert node_rels.shape == (K, K)
  assert data_logmutrel.rels.shape == (K-1, K-1, len(Models._all))
  assert list(data_logmutrel.vids) == ['S%s' % idx for idx in range(K-1)]

  idxs = np.broadcast_to(np.arange(K-1), shape=(K-1, K-1))
  rows = idxs.T
  cols = idxs
  clust_rels = node_rels[1:,1:]
  assert rows.shape == cols.shape == clust_rels.shape

  tree_logmutrel = data_logmutrel.rels[rows,cols,clust_rels]
  for axis in (0, 1):
    tree_logmutrel = np.insert(tree_logmutrel, 0, 0, axis=axis)

  assert np.array_equal(tree_logmutrel, tree_logmutrel.T)
  assert np.all(tree_logmutrel <= 0)
  return tree_logmutrel

def _make_W_dests_mutrel(subtree_head, curr_parent, adj, anc, depth_frac, data_logmutrel):
  assert subtree_head > 0
  assert adj[curr_parent,subtree_head] == 1
  cluster_idx = subtree_head - 1
  assert data_logmutrel.vids[cluster_idx] == 'S%s' % cluster_idx
  K = len(adj)

  logweights = np.full(K, -np.inf)
  for dest in range(K):
    if dest in (curr_parent, subtree_head):
      continue
    new_adj = _modify_tree(adj, anc, dest, subtree_head)
    tree_logmutrel = _calc_tree_logmutrel(adj, data_logmutrel)
    logweights[dest] = np.sum(np.triu(tree_logmutrel))

  assert not np.any(np.isnan(logweights))
  valid_logweights = np.delete(logweights, (curr_parent, subtree_head))
  assert not np.any(np.isinf(valid_logweights))

  weights = util.softmax(logweights)
  # Since we end up taking logs, this can't be exactly zero. If the logweight
  # is extremely negative, then this would otherwise be exactly zero.
  weights = np.maximum(1e-10, weights)
  weights[curr_parent] = 0
  weights[subtree_head] = 0
  weights /= np.sum(weights)
  return weights

def _make_W_dests_uniform(subtree_head, curr_parent, adj):
  K = len(adj)
  weights = np.ones(K)
  weights[subtree_head] = 0
  weights[curr_parent] = 0
  weights /= np.sum(weights)
  return weights

def _sample_cat(W):
  assert np.all(W >= 0) and np.isclose(1, np.sum(W))
  choice = np.random.choice(len(W), p=W)
  assert W[choice] > 0
  return choice

def _load_truth(truthfn):
  import pickle
  with open(truthfn, 'rb') as F:
    truth = pickle.load(F)
  true_adjm = truth['adjm']
  true_phi = truth['phi']
  return (true_adjm, true_phi)

def _ensure_valid_tree(adj):
  # I had several issues with subtle bugs in my tree initialization algorithm
  # creating invalid trees. This function is useful to ensure that `adj`
  # corresponds to a valid tree.
  adj = np.copy(adj)
  K = len(adj)
  assert np.all(np.diag(adj) == 1)
  np.fill_diagonal(adj, 0)
  visited = set()

  stack = [0]
  while len(stack) > 0:
    P = stack.pop()
    assert P not in visited
    visited.add(P)
    C = list(np.flatnonzero(adj[P]))
    stack += C
  assert visited == set(range(K))

def _init_chain(seed, data_mutrel, data_logmutrel, __calc_phi, __calc_llh_phi):
  # Ensure each chain gets a new random state. I add chain index to initial
  # random seed to seed a new chain, so I must ensure that the seed is still in
  # the valid range [0, 2**32).
  np.random.seed(seed % 2**32)

  # Particularly since clusters may not be ordered by mean VAF, a branching
  # tree in which every node comes off the root is the least biased
  # initialization, as it doesn't require any steps that "undo" bad choices, as
  # in the linear or random (which is partly linear, given that later clusters
  # aren't allowed to be parents of earlier ones) cases.
  #init_adj = _init_cluster_adj_branching(K)

  # TODO: change init to work with `data_logmutrel` instead of `data_mutrel`
  init_adj = _init_cluster_adj_mutrels(data_mutrel)
  _ensure_valid_tree(init_adj)

  init_anc = common.make_ancestral_from_adj(init_adj)
  init_depth_frac = _calc_depth_frac(init_adj)
  init_phi = __calc_phi(init_adj)

  init_samp = TreeSample(
    adj = init_adj,
    anc = init_anc,
    depth_frac = init_depth_frac,
    phi = init_phi,
    llh_phi = __calc_llh_phi(init_adj, init_phi),
  )
  return init_samp

def _combine_arrays(arr1, arr2, combiner):
  assert arr1.shape == arr2.shape
  stacked = np.vstack((arr1, arr2))
  combined = np.dot(combiner, stacked)[0]
  assert combined.shape == arr1.shape
  return (stacked, combined)

def _make_W_nodes_combined(adj, data_logmutrel, combiner):
  W_nodes_uniform = _make_W_nodes_uniform(adj)
  W_nodes_mutrel = _make_W_nodes_mutrel(adj, data_logmutrel)
  W_nodes_stacked, W_nodes = _combine_arrays(W_nodes_uniform, W_nodes_mutrel, combiner)
  assert W_nodes[0] == 0
  assert np.isclose(1, np.sum(W_nodes))
  return (W_nodes_stacked, W_nodes)

def _make_W_dests_combined(subtree_head, curr_parent, adj, anc, depth_frac, data_logmutrel, combiner):
  W_dests_uniform = _make_W_dests_uniform(subtree_head, curr_parent, adj)
  W_dests_mutrel = _make_W_dests_mutrel(subtree_head, curr_parent, adj, anc, depth_frac, data_logmutrel)
  W_dests_stacked, W_dests = _combine_arrays(W_dests_uniform, W_dests_mutrel, combiner)
  assert W_dests[subtree_head] == W_dests[curr_parent] == 0
  assert np.isclose(1, np.sum(W_dests))
  return (W_dests_stacked, W_dests)

def _generate_new_sample(old_samp, data_logmutrel, __calc_phi, __calc_llh_phi):
  mode_weights = np.array([hparams.gamma, 1 - hparams.gamma])
  # mode == 0: make uniform update
  # mode == 1: make mutrel-informed update
  mode = _sample_cat(mode_weights)
  combiner = np.array(mode_weights)[np.newaxis,:]

  W_nodes_stacked_old, W_nodes_old = _make_W_nodes_combined(old_samp.adj, data_logmutrel, combiner)
  B = _sample_cat(W_nodes_stacked_old[mode])
  old_parent = _find_parent(B, old_samp.adj)
  W_dests_stacked_old, W_dests_old = _make_W_dests_combined(
    B,
    old_parent,
    old_samp.adj,
    old_samp.anc,
    old_samp.depth_frac,
    data_logmutrel,
    combiner
  )

  A = _sample_cat(W_dests_stacked_old[mode])
  new_adj = _modify_tree(old_samp.adj, old_samp.anc, A, B)
  new_parent = _find_parent(B, new_adj)
  new_anc = common.make_ancestral_from_adj(new_adj)
  new_depth_frac = _calc_depth_frac(new_adj)
  new_phi = __calc_phi(new_adj)
  new_samp = TreeSample(
    adj = new_adj,
    anc = new_anc,
    depth_frac = new_depth_frac,
    phi = new_phi,
    llh_phi = __calc_llh_phi(new_adj, new_phi),
  )

  _, W_nodes_new = _make_W_nodes_combined(new_samp.adj, data_logmutrel, combiner)
  _, W_dests_new = _make_W_dests_combined(
    B,
    new_parent,
    new_samp.adj,
    new_samp.anc,
    new_samp.depth_frac,
    data_logmutrel,
    combiner,
  )

  log_p_new_given_old  = np.log(W_nodes_old[B]) + np.log(W_dests_old[A])
  log_p_old_given_new = np.log(W_nodes_new[B]) + np.log(W_dests_new[old_parent])
  return (new_samp, log_p_new_given_old, log_p_old_given_new)

def _run_chain(data_mutrel, supervars, superclusters, nsamples, thinned_frac, phi_method, phi_iterations, seed, progress_queue=None):
  assert nsamples > 0
  data_logmutrel = _make_data_logmutrel(data_mutrel)

  V, N, omega_v = calc_binom_params(supervars)
  def __calc_phi(adj):
    phi, eta = phi_fitter.fit_phis(adj, superclusters, supervars, method=phi_method, iterations=phi_iterations, parallel=0)
    return phi
  def __calc_llh_phi(adj, phi):
    return _calc_llh_phi(phi, V, N, omega_v)

  samps = [_init_chain(seed, data_mutrel, data_logmutrel, __calc_phi, __calc_llh_phi)]
  accepted = 0
  if progress_queue is not None:
    progress_queue.put(0)

  assert 0 < thinned_frac <= 1
  record_every = round(1 / thinned_frac)
  # Why is `expected_total_trees` equal to this?
  #
  # We always taken the first tree, since `0%k = 0` for all `k`. There remain
  # `nsamples - 1` samples to take, of which we record every `record_every`
  # one.
  #
  # This can give somewhat weird results, since you intuitively expect
  # approximately `thinned_frac * nsamples` trees to be returned. E.g., if
  # `nsamples = 3000` and `thinned_frac = 0.3`, you expect `0.3 * 3000 = 900`
  # trees, but you actually get 1000. To not be surprised by this, try to
  # choose `thinned_frac` such that `1 / thinned_frac` is close to an integer.
  # (I.e., `thinned_frac = 0.5` or `thinned_frac = 0.3333333` generally give
  # results as you'd expect.
  expected_total_trees = 1 + math.floor((nsamples - 1) / record_every)

  old_samp = samps[0]
  for I in range(1, nsamples):
    if progress_queue is not None:
      progress_queue.put(I)

    new_samp, log_p_new_given_old, log_p_old_given_new = _generate_new_sample(
      old_samp,
      data_logmutrel,
      __calc_phi,
      __calc_llh_phi,
    )
    log_p_transition = (new_samp.llh_phi - old_samp.llh_phi) + (log_p_old_given_new - log_p_new_given_old)
    U = np.random.uniform()
    accept = log_p_transition >= np.log(U)
    if accept:
      samp = new_samp
    else:
      samp = old_samp

    if I % record_every == 0:
      samps.append(samp)
    old_samp = samp
    if accept:
      accepted += 1

    def _print_debug():
      true_adj, true_phi = _load_truth(common.debug._truthfn)
      norm_phi_llh = -old_samp.phi.size * np.log(2)
      cols = (
        'iter',
        'action',
        'old_llh',
        'new_llh',
        'true_llh',
        'p_new_given_old',
        'p_old_given_new',
        'old_parents',
        'new_parents',
        'true_parents',
      )
      vals = (
        I,
        'accept' if accept else 'reject',
        '%.3f' % (old_samp.llh_phi / norm_phi_llh),
        '%.3f' % (new_samp.llh_phi / norm_phi_llh),
        '%.3f' % (__calc_llh_phi(true_adj, true_phi) / norm_phi_llh),
        '%.3f' % log_p_new_given_old,
        '%.3f' % log_p_old_given_new,
        _find_parents(old_samp.adj),
        _find_parents(new_samp.adj),
        _find_parents(true_adj),
      )
      debug(*['%s=%s' % (K, V) for K, V in zip(cols, vals)], sep='\t')
    #_print_debug()

  accept_rate = accepted / (nsamples - 1)
  assert len(samps) == expected_total_trees
  debug('accept_rate=%s' % accept_rate, 'total_trees=%s' % len(samps))
  return (
    [S.adj     for S in samps],
    [S.phi     for S in samps],
    [S.llh_phi for S in samps],
  )

def use_existing_structures(adjms, supervars, superclusters, phi_method, phi_iterations, parallel=0):
  V, N, omega_v = calc_binom_params(supervars)
  phis = []
  llhs = []

  for adjm in adjms:
    phi, eta = phi_fitter.fit_phis(adjm, superclusters, supervars, method=phi_method, iterations=phi_iterations, parallel=parallel)
    llh = _calc_llh_phi(phi, V, N, omega_v)
    phis.append(phi)
    llhs.append(llh)
  return (np.array(adjms), np.array(phis), np.array(llhs))

def sample_trees(data_mutrel, supervars, superclusters, trees_per_chain, burnin, nchains, thinned_frac, phi_method, phi_iterations, seed, parallel):
  assert nchains > 0
  assert trees_per_chain > 0
  assert 0 <= burnin <= 1
  assert 0 < thinned_frac <= 1

  jobs = []
  total = nchains * trees_per_chain

  # Don't use (hard-to-debug) parallelism machinery unless necessary.
  if parallel > 0:
    import concurrent.futures
    import multiprocessing
    manager = multiprocessing.Manager()
    # What is stored in progress_queue doesn't matter. The queue is just used
    # so that child processes can signal when they've sampled a tree, allowing
    # the main process to update the progress bar.
    progress_queue = manager.Queue()
    with progressbar(total=total, desc='Sampling trees', unit='tree', dynamic_ncols=True) as pbar:
      with concurrent.futures.ProcessPoolExecutor(max_workers=parallel) as ex:
        for C in range(nchains):
          # Ensure each chain's random seed is different from the seed used to
          # seed the initial Pairtree invocation, yet nonetheless reproducible.
          jobs.append(ex.submit(_run_chain, data_mutrel, supervars, superclusters, trees_per_chain, thinned_frac, phi_method, phi_iterations, seed + C + 1, progress_queue))

        # Exactly `total` items will be added to the queue. Once we've
        # retrieved that many items from the queue, we can assume that our
        # child processes are finished sampling trees.
        for _ in range(total):
          # Block until there's something in the queue for us to retrieve,
          # indicating a child process has sampled a tree.
          progress_queue.get()
          pbar.update()

    results = [J.result() for J in jobs]
  else:
    results = []
    for C in range(nchains):
      results.append(_run_chain(data_mutrel, supervars, superclusters, trees_per_chain, thinned_frac, phi_method, phi_iterations, seed + C + 1))

  discard_first = round(burnin * trees_per_chain)
  merged_adj = []
  merged_phi = []
  merged_llh = []
  for A, P, L in results:
    merged_adj += A[discard_first:]
    merged_phi += P[discard_first:]
    merged_llh += L[discard_first:]
  assert len(merged_adj) == len(merged_phi) == len(merged_llh)
  return (merged_adj, merged_phi, merged_llh)