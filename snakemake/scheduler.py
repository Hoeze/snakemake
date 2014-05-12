# -*- coding: utf-8 -*-

import os
import threading
import multiprocessing
import operator
from functools import partial
from collections import defaultdict
from itertools import chain, accumulate

from snakemake.executors import DryrunExecutor, TouchExecutor
from snakemake.executors import GenericClusterExecutor, CPUExecutor, DRMAAExecutor
from snakemake.logging import logger

__author__ = "Johannes Köster"



def cumsum(iterable, zero=[0]):
    return list(chain(zero, accumulate(iterable)))


class JobScheduler:
    def __init__(
        self,
        workflow,
        dag,
        cores,
        dryrun=False,
        touch=False,
        cluster=None,
        drmaa=False,
        drmaa_args="",
        jobname=None,
        immediate_submit=False,
        quiet=False,
        printreason=False,
        printshellcmds=False,
        keepgoing=False,
        latency_wait=3,):
        """ Create a new instance of KnapsackJobScheduler. """
        self.cluster = cluster
        self.dag = dag
        self.workflow = workflow
        self.dryrun = dryrun
        self.quiet = quiet
        self.keepgoing = keepgoing
        self.running = set()
        self.failed = set()
        self.finished_jobs = 0

        self.resources = dict(self.workflow.global_resources)

        use_threads = os.name != "posix" or cluster
        if not use_threads:
            self._open_jobs = multiprocessing.Event()
            self._lock = multiprocessing.Lock()
        else:
            self._open_jobs = threading.Event()
            self._lock = threading.Lock()
        self._errors = False
        self._finished = False
        self._job_queue = None
        self._submit_callback = self._noop
        self._finish_callback = partial(
            self._proceed,
            update_dynamic=not self.dryrun,
            print_progress=not self.quiet and not self.dryrun)

        if dryrun:
            self._executor = DryrunExecutor(
                workflow, dag, printreason=printreason,
                quiet=quiet, printshellcmds=printshellcmds,
                latency_wait=latency_wait)
            self.rule_reward = self.dryrun_rule_reward
        elif touch:
            self._executor = TouchExecutor(
                workflow, dag, printreason=printreason,
                quiet=quiet, printshellcmds=printshellcmds,
                latency_wait=latency_wait)
        elif cluster or drmaa:
            # TODO properly set cores
            self._local_executor = CPUExecutor(
                workflow, dag, cores, printreason=printreason,
                quiet=quiet, printshellcmds=printshellcmds,
                threads=use_threads,
                latency_wait=latency_wait)
            if cluster:
                self._executor = GenericClusterExecutor(
                    workflow, dag, None, submitcmd=cluster, jobname=jobname,
                    printreason=printreason, quiet=quiet,
                    printshellcmds=printshellcmds, latency_wait=latency_wait)
                if immediate_submit:
                    self.rule_reward = self.dryrun_rule_reward
                    self._submit_callback = partial(
                        self._proceed,
                        update_dynamic=False,
                        print_progress=False,
                        update_resources=False)
                else:
                    self.run = self.run_cluster_or_local
            else:
                self._executor = DRMAAExecutor(
                    workflow, dag, None, drmaa_args=drmaa_args, jobname=jobname,
                    printreason=printreason, quiet=quiet,
                    printshellcmds=printshellcmds, latency_wait=latency_wait)
                self.run = self.run_cluster_or_local
        else:
            self._executor = CPUExecutor(
                workflow, dag, cores, printreason=printreason,
                quiet=quiet, printshellcmds=printshellcmds,
                threads=use_threads,
                latency_wait=latency_wait)
        self._open_jobs.set()

    @property
    def stats(self):
        try:
            return self._executor.stats
        except AttributeError:
            raise TypeError("Executor does not support stats")

    def candidate(self, job):
        """ Return whether a job is a candidate to be executed. """
        return (job not in self.running and job not in self.failed and (self.dryrun or
            (not job.dynamic_input and not self.dag.dynamic(job))))

    @property
    def open_jobs(self):
        """ Return open jobs. """
        return filter(self.candidate, list(self.dag.ready_jobs))

    def schedule(self):
        """ Schedule jobs that are ready, maximizing cpu usage. """
        while True:
            try:
                # work around so that the wait does not prevent keyboard interrupts
                while not self._open_jobs.wait(1): pass
            except:
                # this will be caused because of SIGTERM or SIGINT
                logger.info("Terminating processes on user request.")
                self._executor.shutdown()
                return False
            self._open_jobs.clear()
            if not self.keepgoing and self._errors:
                logger.info("Will exit after finishing "
                    "currently running jobs.")
                self._executor.shutdown()
                return False
            if not any(self.open_jobs):
                self._executor.shutdown()
                return not self._errors

            needrun = list(self.open_jobs)
            assert needrun

            logger.debug("Resources before job selection: {}".format(self.resources))
            logger.debug("Ready jobs ({}):\n\t".format(len(needrun)) + "\n\t".join(map(str, needrun)))

            run = self.job_selector(needrun)
            logger.debug("Selected jobs ({}):\n\t".format(len(run)) + "\n\t".join(map(str, run)))
            self.running.update(run)
            logger.debug("Resources after job selection: {}".format(self.resources))
            for job in run:
                self.run(job)

    def run(self, job):
        self._executor.run(
            job, callback=self._finish_callback,
            submit_callback=self._submit_callback,
            error_callback=self._error)

    def run_cluster_or_local(self, job):
        executor = self._local_executor if self.workflow.is_local(job.rule) else self._executor
        executor.run(
            job, callback=self._finish_callback,
            submit_callback=self._submit_callback,
            error_callback=self._error)

    def _noop(self, job):
        pass

    def _proceed(
        self, job, update_dynamic=True, print_progress=False,
        update_resources=True):
        """ Do stuff after job is finished. """
        with self._lock:
            if update_resources:
                self.finished_jobs += 1
                self.running.remove(job)
                for name, value in job.resources.items():
                    if name in self.resources:
                        self.resources[name] += value
                        logger.debug("Releasing {} {} (now {}).".format(value, name, self.resources[name]))

            self.dag.finish(job, update_dynamic=update_dynamic)

            if print_progress:
                self.progress()

            if any(self.open_jobs) or not self.running:
                # go on scheduling if open jobs are ready or no job is running
                self._open_jobs.set()

    def _error(self, job):
        """ Clear jobs and stop the workflow. """
        with self._lock:
            self._errors = True
            self.running.remove(job)
            self.failed.add(job)
            if self.keepgoing:
                logger.info("Job failed, going on with independent jobs.")
            else:
                self._open_jobs.set()

    def job_selector(self, jobs, alpha=1):
        """
        Using the greedy heuristic from
        "A Greedy Algorithm for the General Multidimensional Knapsack
Problem", Akcay, Li, Xu, Annals of Operations Research, 2012

        Args:
            jobs (list):    list of jobs
            alpha (float):  greedyness (1 means take all possible jobs for a selected rule)
        """
        with self._lock:
            # solve over the rules instead of jobs (much less)
            _jobs = defaultdict(list)
            for job in jobs:
                _jobs[job.rule].append(job)
            jobs = _jobs
            # sort the jobs by priority
            for _jobs in jobs.values():
                _jobs.sort(key=self.dag.priority, reverse=True)
            rules = list(jobs)

            # Step 1: initialization
            n = len(rules)
            x = [0] * n  # selected jobs of each rule
            E = set(range(n))  # rules free to select
            u = [len(jobs[rule]) for rule in rules]  # number of jobs left
            b = list(self.resources.values())  # resource capacities
            a = list(map(self.rule_weight, rules))  # resource usage of rules
            c = list(map(partial(self.rule_reward, jobs=jobs), rules))  # matrix of cumulative rewards over jobs

            while True:
                # Step 2: compute effective capacities
                y = [
                        (
                            min(
                                (min(u[j], b_i // a_j_i) if a_j_i > 0 else u[j])
                                for b_i, a_j_i in zip(b, a[j]) if a_j_i
                            )
                            if j in E else 0
                        )
                        for j in range(n)
                    ]
                if not any(y):
                    break
                y = [(max(1, alpha * y_j) if y_j > 0 else 0) for y_j in y]

                # Step 3: compute rewards on cumulative sums and normalize by y
                # in order to not prefer rules with small weights / many jobs
                reward = [(
                    [(crit[x_j + y_j] - crit[x_j]) for crit in c_j]
                    if j in E else [0] * len(c_j))
                    for j, (c_j, y_j, x_j) in enumerate(zip(c, y, x))]
                j_sel = max(E, key=reward.__getitem__)  # argmax

                # Step 4: batch increment
                y_sel = y[j_sel]

                # Step 5: update information
                x[j_sel] += y_sel
                b = [b_i - (a_j_i * y_sel) for b_i, a_j_i in zip(b, a[j_sel])]
                u[j_sel] -= y_sel
                if not u[j_sel] or alpha == 1:
                    E.remove(j_sel)
                if not E:
                    break

            # Solution is the list of jobs that was selected from the selected rules
            solution = list(chain(
                *[jobs[rules[j]][:x_] for j, x_ in enumerate(x)]))
            # update resources
            for name, b_i in zip(self.resources, b):
                self.resources[name] = b_i
            return solution

    def rule_weight(self, rule):
        res = rule.resources
        def calc_res(name, value):
            return min(res.get(name, 0), value)

        return [calc_res(*item) for item in self.workflow.global_resources.items()]

    def rule_reward(self, rule, jobs=None):
        jobs = jobs[rule]
        return (
            self.priority_reward(jobs),
            self.downstream_reward(jobs),
            cumsum([job.inputsize for job in jobs]))

    def dryrun_rule_reward(self, rule, jobs=None):
        jobs = jobs[rule]
        return (
            self.priority_reward(jobs),
            self.downstream_reward(jobs),
            [0] * (len(jobs) + 1))

    def priority_reward(self, jobs):
        return cumsum(self.dag.priorities(jobs))

    def downstream_reward(self, jobs):
        return cumsum(self.dag.downstream_sizes(jobs))

    def job_weight(self, job):
        """ Job weight that uses threads. """
        return job.threads

    def simple_job_weight(self, job):
        """ Job weight that ignores threads. """
        return 1

    def progress(self):
        """ Display the progress. """
        logger.progress(done=self.finished_jobs, total=len(self.dag))
