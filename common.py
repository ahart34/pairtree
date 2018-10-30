import numpy as np
from collections import namedtuple

import warnings
with warnings.catch_warnings():
  warnings.simplefilter('ignore', category=DeprecationWarning)
  import sklearn.cluster

_LOGEPSILON = -400
_EPSILON    = np.exp(_LOGEPSILON)

Variant = namedtuple('Variant', (
  'id',
  'var_reads',
  'ref_reads',
  'total_reads',
  'vaf',
  'omega_v',
))

def convert_variant_dict_to_tuple(V):
  return Variant(**{K: V[K] for K in Variant._fields})

class Models:
  _all = ('garbage', 'cocluster', 'A_B', 'B_A', 'diff_branches')
for idx, M in enumerate(Models._all):
  setattr(Models, M, idx)

def make_ancestral_from_adj(adj):
  K = len(adj)
  Z = np.zeros((K,K))

  def _find_desc(I, vec):
    # Base case: if I have no children, my ancestor vec is just myself.
    if np.sum(vec) == 0:
      return vec
    else:
      children = np.array([_find_desc(idx, adj[idx]) for (idx, val) in enumerate(vec) if idx != I and val == 1])
      self_and_child = vec + np.sum(children, axis=0)
      self_and_child[self_and_child > 1] = 1
      return self_and_child

  for k in range(K):
    # If we know `adj` is topologically sorted, we can reduce the complexity of
    # this -- we would start at leaves and work our way upward, eliminating
    # need for recursive DFS. But since we don't expect `K` to be large, we can
    # write more general version that works for non-toposorted trees.
    Z[k] = _find_desc(k, adj[k])

  return Z

def make_cluster_supervars(clusters, variants):
  cluster_supervars = {}

  for cidx, cluster in enumerate(clusters):
    if len(cluster) == 0:
      continue
    cvars = [variants['s%s' % vidx] for vidx in cluster]

    cluster_total_reads = np.array([V['total_reads'] for V in cvars])
    cluster_var_reads = np.array([V['var_reads'] for V in cvars])
    # Correct for sex variants.
    omega_v = np.array([V['omega_v'] for V in cvars])[:,np.newaxis]
    cluster_var_reads = np.round(cluster_var_reads / (2*omega_v))

    S_name = 'C%s' % len(cluster_supervars)
    S = {
      'id': S_name,
      'name': S_name,
      'chrom': None,
      'pos': None,
      'cluster': cidx,
      'omega_v': 0.5,
      'var_reads': np.sum(cluster_var_reads, axis=0),
      'total_reads': np.sum(cluster_total_reads, axis=0),
    }
    S['ref_reads'] = S['total_reads'] - S['var_reads']
    S['vaf'] = S['var_reads'] / S['total_reads']

    cluster_supervars[S_name] = S

  return cluster_supervars

def make_superclusters(supervars):
  N = len(supervars)
  superclusters = [[C] for C in range(N)]
  superclusters.insert(0, [])
  return superclusters

def agglo_children_to_adjlist(children, nleaves):
  assert len(children) == nleaves - 1
  adjlist = {}
  for idx, C in enumerate(children):
    adjlist[idx + nleaves] = C
  root = nleaves + len(children) - 1
  assert max(adjlist.keys()) == root
  return (adjlist, root)

def dfs(adjlist, root):
  ordered = []
  def _dfs(A, parent):
    if parent not in A:
      ordered.append(parent)
      return
    for child in A[parent]:
      _dfs(A, child)
  _dfs(adjlist, root)
  return np.array(ordered)

def reorder_rows(mat, start=None, end=None):
  N = len(mat)
  if start is None:
    start = 0
  if end is None:
    end = N

  fullidxs = np.array(range(N))
  submat = mat[start:end]

  # n_clusters doesn't matter, as we're only interested in the linkage tree
  # between data points.
  agglo = sklearn.cluster.AgglomerativeClustering(
    n_clusters = len(submat),
    affinity = 'l2',
    linkage = 'average',
    compute_full_tree = True,
  )
  labels = agglo.fit_predict(submat)
  adjlist, root = agglo_children_to_adjlist(agglo.children_, agglo.n_leaves_)
  idxs = dfs(adjlist, root)
  submat = [submat[I] for I in idxs]

  fullidxs[start:end] = idxs + start
  mat = np.vstack((mat[:start], submat, mat[end:]))
  return (mat, fullidxs)

def reorder_cols(mat, start=None, end=None):
  # Disable reordering for Pieter's data, since he has a defined sample order
  # he needs.
  #return (mat, np.array(range(len(mat.T))))
  mat = mat.T
  mat, idxs = reorder_rows(mat, start, end)
  return (mat.T, idxs)

def reorder_square_matrix(mat):
  # Reorders the rows, then reorders the columns using the same indices. This
  # works for symmetric matrices. It can also be used for non-symmetric
  # matrices, as it doesn't require symmetry.
  assert mat.shape[0] == mat.shape[1]
  mat, idxs = reorder_rows(mat)
  mat = mat.T[idxs,:].T
  return (mat, idxs)

def is_xeno(samp):
  return 'xeno' in samp.lower()

def extract_patient_samples(variants, sampnames):
  munged = {}
  patient_mask = np.array([not is_xeno(S) for S in sampnames])
  for vid in variants.keys():
    munged[vid] = dict(variants[vid])
    for K in ('total_reads', 'ref_reads', 'var_reads', 'vaf'):
      munged[vid][K] = variants[vid][K][patient_mask]
  variants = munged
  sampnames = [S for (S, is_patient) in zip(sampnames, patient_mask) if is_patient]
  return (variants, sampnames)

def debug(*args, **kwargs):
  if debug.DEBUG:
    print(*args, **kwargs)
debug.DEBUG = False
