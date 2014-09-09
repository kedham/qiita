#!/usr/bin/env python
from __future__ import division
from json import dumps
from os.path import join
from sys import stderr

from redis import Redis
import networkx as nx

from qiita_db.job import Job
from qiita_db.util import get_db_files_base_dir
from qiita_ware.wrapper import ParallelWrapper
from qiita_ware.context import system_call
from qiita_ware.exceptions import ComputeError


# -----------------------------------------------------------------------------
# Copyright (c) 2014--, The Qiita Development Team.
#
# Distributed under the terms of the BSD 3-clause License.
#
# The full license is in the file LICENSE, distributed with this software.
# -----------------------------------------------------------------------------

def _job_comm_wrapper(user, analysis_id, job):
    """Wraps the job command execution to allow redis communication"""
    name, command = job.command
    options = job.options
    # create json base for websocket messages
    msg = {
        "analysis": analysis_id,
        "msg": None,
        "command": "%s: %s" % (job.datatype, name)
    }

    o_fmt = ' '.join(['%s %s' % (k, v) for k, v in options.items()])
    c_fmt = str("%s %s" % (command, o_fmt))

    # send running message to user wait page
    r_server = Redis()
    job.status = 'running'
    msg["msg"] = "Running"
    r_server.rpush(user + ":messages", dumps(msg))
    r_server.publish(user, dumps(msg))

    # run the command
    try:
        system_call(c_fmt)
    except Exception as e:
        job.status = 'error'
        msg["msg"] = "ERROR"
        r_server.rpush(user + ":messages", dumps(msg))
        r_server.publish(user, dumps(msg))
        print("Failed compute on job id %d:\n%s\n%s" % (job.id, c_fmt, str(e)))
        return

    msg["msg"] = "Completed"
    r_server = Redis()
    r_server.rpush(user + ":messages", dumps(msg))
    r_server.publish(user, dumps(msg))
    # FIX THIS Should not be hard coded
    job.add_results([(job.options["--output_dir"], "directory")])
    job.status = 'completed'


def _build_analysis_files(analysis, r_depth=None):
    """Creates the biom tables and mapping file, then adds to jobs"""
    # create the biom tables and add jobs to the analysis
    analysis.status = "running"
    analysis.build_files(r_depth)
    mapping_file = analysis.mapping_file
    biom_tables = analysis.biom_tables

    # add files to existing jobs
    for job_id in analysis.jobs:
        job = Job(job_id)
        if job.status == 'queued':
            opts = {
                "--otu_table_fp": biom_tables[job.datatype],
                "--mapping_fp": mapping_file
            }
            job_opts = job.options
            job_opts.update(opts)
            job.options = job_opts


def _finish_analysis(user, analysis):
    # check job exit statuses for analysis result status
    all_good = True
    for job_id in analysis.jobs:
        if Job(job_id).status == "error":
            all_good = False
            break

    # set final analysis status
    if all_good:
        analysis.status = "completed"
    else:
        analysis.status = "error"

    # send websockets message that we are done running all jobs
    msg = {
        "msg": "allcomplete",
        "analysis": analysis.id
    }
    r_server = Redis()
    r_server.rpush(user + ":messages", dumps(msg))
    r_server.publish(user, dumps(msg))


class RunAnalysis(ParallelWrapper):
    def __init__(self):
        super(RunAnalysis, self).__init__(block=False)

    def _construct_job_graph(self, user, analysis, commands, comm_opts=None,
                             rarefaction_depth=None):
        self._logger = stderr
        # Add jobs to analysis
        if comm_opts is None:
            comm_opts = {}
        for data_type, command in commands:
            # get opts set by user, else make it empty dict
            opts = comm_opts.get(command, {})
            # Add commands to analysis as jobs
            # HARD CODED HACKY THING FOR DEMO, FIX  Issue #164
            if command == "Beta Diversity" and data_type in {'16S', '18S'}:
                opts["--tree_fp"] = join(get_db_files_base_dir(), "reference",
                                         "gg_97_otus_4feb2011.tre")
            elif command == "Beta Diversity":
                opts["--parameter_fp"] = join(get_db_files_base_dir(),
                                              "reference", "params_qiime.txt")
            Job.create(data_type, command, opts, analysis,
                       return_existing=True)

        # Create the files for the jobs
        files_node_name = "%d_ANALYSISFILES" % analysis.id
        self._job_graph.add_node(files_node_name,
                                 job=(_build_analysis_files,
                                      analysis, rarefaction_depth),
                                 requires_deps=False)
        # Add the jobs
        job_nodes = []
        for job_id in analysis.jobs:
            job = Job(job_id)
            node_name = "%d_JOB_%d" % (analysis.id, job.id)
            job_nodes.append(node_name)
            self._job_graph.add_node(node_name,
                                     job=(_job_comm_wrapper, user, analysis.id,
                                          job),
                                     requires_deps=False)
            # Adding the dependency edges to the graph
            self._job_graph.add_edge(files_node_name, node_name)

        # Finalize the analysis
        node_name = "FINISH_ANALYSIS_%d" % analysis.id
        self._job_graph.add_node(node_name,
                                 job=(_finish_analysis, user, analysis),
                                 requires_deps=False)
        # Adding the dependency edges to the graph
        for job_node_name in job_nodes:
            self._job_graph.add_edge(job_node_name, node_name)
