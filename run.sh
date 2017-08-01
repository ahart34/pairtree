#!/bin/sh
set -euo pipefail

PROTDIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
RUNNAME=xenos
BASEDIR=~/work/steph
SSMDIR=$BASEDIR/data/inputs/steph.xenos.nocns
OUTDIR=$BASEDIR/data/pairwise.$RUNNAME
HANDBUILTDIR=$BASEDIR/data/handbuilt_trees
RENAMEDSAMPS=$BASEDIR/misc/renamed.txt
HIDDENSAMPS=$BASEDIR/misc/hidden.txt
PWGSDIR=~/.apps/phylowgs

OUTPUT_TYPES="clustered unclustered condensed"
OUTPUT_TYPES="clustered"

function remove_samples {
  for paramsfn in $SSMDIR/*.params.json; do
    sampid=$(basename $paramsfn | cut -d . -f1)
    echo "python3 $PROTDIR/remove_samples.py" \
      "$sampid" \
      "$SSMDIR/$sampid.sampled.ssm" \
      "$paramsfn"
  done | parallel -j40 --halt 1
}

function rename_samples {
  for paramsfn in $SSMDIR/*.params.json; do
    sampid=$(basename $paramsfn | cut -d . -f1)
    echo "python3 $PROTDIR/rename_samples.py" \
      "$sampid" \
      "$HIDDENSAMPS" \
      "$RENAMEDSAMPS" \
      "$paramsfn"
  done | parallel -j40 --halt 1
}

function calc_pairwise {
  rm -f $OUTDIR/*.{pairwise.json,stdout,stderr}

  for ssmfn in $SSMDIR/*.sampled.ssm; do
    sampid=$(basename $ssmfn | cut -d . -f1)
    echo "python3 $PROTDIR/pairwise.py "\
      "$ssmfn" \
      "$OUTDIR/$sampid.pairwise.json" \
      "> $OUTDIR/$sampid.stdout" \
      "2> $OUTDIR/$sampid.stderr"
  done | parallel -j40 --halt 1
}

function plot {
  rm -f $OUTDIR/*.{pairwise.html,js}

  cp -a $PROTDIR/highlight_table_labels.js $OUTDIR/
  for jsonfn in $OUTDIR/*.pairwise.json; do
    sampid=$(basename $jsonfn | cut -d . -f1)
    ssmfn=$SSMDIR/$sampid.sampled.ssm
    paramsfn=$SSMDIR/$sampid.params.json
    spreadsheetfn=$BASEDIR/data/ssms/$sampid.csv
    handbuiltfn="$HANDBUILTDIR/$sampid.json"

    [ -f $handbuiltfn ] || continue

    for output_type in $OUTPUT_TYPES; do
      echo "python3 $PROTDIR/plot.py " \
	"--output-type $output_type " \
	"$sampid" \
	"$jsonfn" \
	"$ssmfn" \
	"$paramsfn" \
	"$spreadsheetfn" \
	"$handbuiltfn" \
	"$OUTDIR/$sampid.$output_type.pairwise.html" \
	"$OUTDIR/$sampid.summ.json" \
	"$OUTDIR/$sampid.muts.json" \
	">  $OUTDIR/$sampid.plot.stdout" \
	"2> $OUTDIR/$sampid.plot.stderr"
    done
  done | parallel -j40 --halt 1
}

function write_index {
  cd $OUTDIR
  for status in $OUTPUT_TYPES; do
    echo "<h3>$status</h3>"
    for htmlfn in S*.$status.pairwise.html; do
      sampid=$(basename $htmlfn | cut -d. -f1)
      echo "<a href=$htmlfn>$sampid</a><br>"
    done
  done > index.html
}

function add_tree_indices {
  for jsonfn in $OUTDIR/*.summ.json; do
    sampid=$(basename $jsonfn | cut -d . -f1)
    gzip "$OUTDIR/$sampid.summ.json" "$OUTDIR/$sampid.muts.json"
    echo "PYTHONPATH=$PWGSDIR python2 $PROTDIR/add_tree_indices.py" \
      "$OUTDIR/$sampid.summ.json.gz" \
      "$OUTDIR/$sampid.muts.json.gz"
  done | parallel -j40 --halt 1
  gunzip $OUTDIR/*.{summ,muts}.json.gz

}

function add_to_witness {
  witnessdir=$PWGSDIR/witness/data/steph.$RUNNAME.$(date '+%Y%m%d')
  mkdir -p $witnessdir
  cp -a $OUTDIR/*.{summ,muts}.json $witnessdir
  cd $PWGSDIR/witness
  python2 index_data.py
}

function main {
  mkdir -p $OUTDIR

  #rename_samples
  #remove_samples

  #calc_pairwise
  plot
  add_tree_indices
  write_index
  add_to_witness
}

main