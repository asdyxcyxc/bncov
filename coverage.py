"""
coverage.py - defines CoverageDB, which encapsulates coverage data and basic methods for loading/presenting that data
"""

from __future__ import division, absolute_import

import os
from . import parse
from collections import namedtuple

try:
    import msgpack
    file_backing_disabled = False
except ImportError:
    file_backing_disabled = True
    # print("[!] bncov: without msgpack module, CoverageDB save/load to file is disabled")

FuncCovStats = namedtuple("FuncCovStats", "coverage_percent blocks_covered blocks_total")


class CoverageDB(object):

    def __init__(self, bv, filename=None):
        self.bv = bv
        self.module_name = os.path.basename(bv.file.original_filename)
        self.module_base = bv.start
        if filename:
            self.load_from_file(filename)
        else:
            # map basic blocks in module to their size, used for disambiguating dynamic block coverage
            self.module_blocks = {bb.start: bb.length for bb in bv.basic_blocks}
            self.block_dict = {}  # map address of start of basic block to list of traces that contain it
            self.total_coverage = set()  # overall coverage set of addresses
            self.coverage_files = []  # list of trace names (filepaths)
            self.trace_dict = {}  # map filename to the set of addrs of basic blocks hit
            self.function_stats = {}  # deferred - populated by self.collect_function_coverage()
            self.frontier = set()  # deferred - populated by self.get_frontier()
            self.filename = ""  # the path to file this covdb is loaded from/saved to ("" otherwise)

    # Save/Load covdb functions
    def save_to_file(self, filename):
        """Save only the bare minimum needed to reconstruct this CoverageDB.

        This serializes the data to a single file and cab reduce the disk footprint of
        block coverage significantly (depending on overlap and number of files)."""
        if file_backing_disabled:
            raise Exception("[!] Can't save/load coverage db files without msgpack. Try `pip install msgpack`")
        save_dict = dict()
        save_dict["version"] = 1  # serialized covdb version
        save_dict["module_name"] = self.module_name
        save_dict["module_base"] = self.module_base
        save_dict["coverage_files"] = self.coverage_files
        # save tighter version of block dict {int: int} vice {int: str}
        block_dict_to_save = {}
        file_index_map = {filepath: self.coverage_files.index(filepath) for filepath in self.coverage_files}
        for block, trace_list in self.block_dict.items():
            trace_id_list = [file_index_map[name] for name in trace_list]
            block_dict_to_save[block] = trace_id_list
        save_dict["block_dict"] = block_dict_to_save
        # write packed version to file
        with open(filename, "wb") as f:
            msgpack.dump(save_dict, f)
            self.filename = filename

    def load_from_file(self, filename):
        """Reconstruct a CoverageDB using the current BinaryView and a CoverageDB saved to disk using .save_to_file()"""
        if file_backing_disabled:
            raise Exception("[!] Can't save/load coverage db files without msgpack. Try `pip install msgpack`")
        self.filename = filename
        with open(filename, "rb") as f:
            loaded_dict = msgpack.load(f, raw=False)
        if "version" not in loaded_dict:
            self._old_load_from_file(loaded_dict)
        # Do sanity checks
        loaded_version = int(loaded_dict["version"])
        if loaded_version != 1:
            raise Exception("[!] Unsupported version number: %d" % loaded_version)

        loaded_module_name = loaded_dict["module_name"]
        if loaded_module_name != self.module_name:
            raise Exception("[!] ERROR: Module name from covdb (%s) doesn't match BinaryView (%s)" %
                            (loaded_module_name, self.module_name))

        loaded_module_base = loaded_dict["module_base"]
        if loaded_module_base != self.module_base:
            raise Exception("[!] ERROR: Module base from covdb (0x%x) doesn't match BinaryView (0x%x)" %
                            (loaded_module_base, self.module_base))

        # Parse the saved members
        coverage_files = loaded_dict["coverage_files"]
        self.coverage_files = coverage_files

        block_dict = dict()
        loaded_block_dict = loaded_dict["block_dict"]
        file_index_map = {self.coverage_files.index(filepath): filepath for filepath in self.coverage_files}
        for block, trace_id_list in loaded_block_dict.items():
            trace_list = [file_index_map[i] for i in trace_id_list]
            block_dict[block] = trace_list
        self.block_dict = block_dict

        # Regen other members from saved members
        bv = self.bv
        self.module_blocks = {bb.start: bb.length for bb in bv.basic_blocks}
        trace_dict = {}
        for block, trace_list in block_dict.items():
            for name in trace_list:
                trace_dict.setdefault(name, set()).add(block)
        self.trace_dict = trace_dict
        self.total_coverage = set(block_dict.keys())

        # Other members are blank/empty
        self.function_stats = {}
        self.frontier = set()

    def _old_load_from_file(self, loaded_object_dict):
        """Backwards compatibility for when version numbers weren't saved"""
        self.module_name = loaded_object_dict["module_name"]
        self.module_base = loaded_object_dict["module_base"]
        self.module_blocks = loaded_object_dict["module_blocks"]
        self.trace_dict = {k: set(v) for k, v in loaded_object_dict["trace_dict"].items()}
        self.block_dict = loaded_object_dict["block_dict"]
        self.function_stats = loaded_object_dict["function_stats"]
        self.coverage_files = loaded_object_dict["coverage_files"]
        self.total_coverage = set(loaded_object_dict["total_coverage"])
        self.frontier = set(loaded_object_dict["frontier"])

    # Coverage import functions
    def add_file(self, filepath):
        """Add a new coverage file"""
        coverage = parse.parse_coverage_file(filepath, self.module_name, self.module_base, self.module_blocks)
        if len(coverage) <= 10:
            print("[!] Warning: Coverage file %s returned very few coverage addresses (%d)"
                  % (filepath, len(coverage)))
        for addr in coverage:
            self.block_dict.setdefault(addr, []).append(filepath)
        self.coverage_files.append(filepath)
        self.trace_dict[filepath] = coverage
        self.total_coverage |= coverage
        return coverage

    def add_directory(self, dirpath):
        """Add directory of coverage files"""
        for filename in os.listdir(dirpath):
            self.add_file(os.path.join(dirpath, filename))

    # Analysis functions
    def get_traces_from_block(self, addr):
        """Return traces that cover the block that contains addr"""
        addr = self.bv.get_basic_blocks_at(addr)[0].start
        return [name for name, trace in self.trace_dict.items() if addr in trace]

    def get_rare_blocks(self, threshold=1):
        """Return a list of blocks that are covered by <= threshold traces"""
        rare_blocks = []
        for block in self.total_coverage:
            count = 0
            for _, trace in self.trace_dict.items():
                if block in trace:
                    count += 1
                    if count > threshold:
                        break
            if count <= threshold:
                rare_blocks.append(block)
        return rare_blocks

    def get_block_rarity_dict(self):
        """Return a mapping of blocks to the # of traces that cover it"""
        return {block: len(self.get_traces_from_block(block)) for block in self.total_coverage}

    def get_functions_from_blocks(self, blocks):
        """Returns a dictionary mapping function names to the block start addrs they contain"""
        functions = {}
        for addr in blocks:
            matching_functions = self.bv.get_functions_containing(addr)
            if not matching_functions:
                print("[!] No functions found containing block start 0x%x" % addr)
            else:
                functions.setdefault(matching_functions[0].name, []).append(addr)
        return functions

    def get_trace_blocks(self, trace_name):
        """Get the set of basic blocks a trace covers"""
        return self.trace_dict[trace_name]

    def get_functions_from_trace(self, trace_name):
        """Get the list of function names a trace covers"""
        return list(self.get_functions_from_blocks(self.trace_dict[trace_name]).keys())

    def get_trace_uniq_blocks(self, trace_name):
        """Get the set of basic blocks that are only seen in the specified trace"""
        return self.trace_dict[trace_name] & set(self.get_rare_blocks())

    def get_trace_uniq_functions(self, trace_name):
        """Get a list of function names containing basic blocks that are only seen in the specified trace"""
        return list(self.get_functions_from_blocks(self.get_trace_uniq_blocks(trace_name)).keys())

    def get_functions_with_rare_blocks(self):
        """Get a list of function names that contain basic blocks only covered by one trace"""
        return list(self.get_functions_from_blocks(self.get_rare_blocks()).keys())

    def get_traces_with_rare_blocks(self):
        """Get the set of traces that have blocks that are unique to them"""
        traces = set()
        for block in self.get_rare_blocks():
            traces.update(self.get_traces_from_block(block))
        return traces

    def get_traces_from_function(self, function_name):
        """Return a set of traces that cover the function specified by function_name"""
        matching_functions = [f for f in self.bv.functions if f.name == function_name]
        if len(matching_functions) == 0:
            print("[!] No functions match %s" % function_name)
            return matching_functions
        if len(matching_functions) > 1:
            raise Exception("[!] Warning, multiple functions matched name: %s" % function_name)
        matching_function = matching_functions[0]
        traces = set()
        for block in matching_function.basic_blocks:
            traces.update(self.get_traces_from_block(block.start))
        return traces

    def get_n_rarest_blocks(self, n):
        blocks_by_rarity = sorted(list(self.block_dict.keys()), key=lambda x: len(self.block_dict[x]))
        return blocks_by_rarity[:n]

    def all_edges_covered(self, addr):
        """Return True if all outgoing edge targets are covered, False otherwise"""
        blocks = self.bv.get_basic_blocks_at(addr)
        for block in blocks:
            if len(block.outgoing_edges) == 1:
                # there could be cases where we don't cover the next block,
                # ignoring for now
                return True
            for edge in block.outgoing_edges:
                if edge.target.start not in self.total_coverage:
                    return False
        return True

    def get_frontier(self):
        """Return a set of addrs of blocks that have an uncovered outgoing edge target"""
        frontier_set = set()
        for addr in self.total_coverage:
            if not self.all_edges_covered(addr):
                frontier_set.add(addr)
        self.frontier = frontier_set
        return frontier_set

    # Statistic report functions
    def collect_function_coverage(self):
        """Collect stats on block coverage within functions (which is by default deferred)"""
        for func in self.bv:
            func_blocks = len(func.basic_blocks)
            blocks_covered = 0
            for block in func.basic_blocks:
                if block.start in self.total_coverage:
                    blocks_covered += 1
            coverage_percent = (blocks_covered / float(func_blocks)) * 100
            cur_stats = FuncCovStats(coverage_percent, blocks_covered, func_blocks)
            self.function_stats[func.name] = cur_stats
        return self.function_stats

    def get_overall_function_coverage(self):
        """Returns (number_of_functions, total_blocks_covered, total_blocks)"""
        if self.function_stats == {}:
            self.collect_function_coverage()
        blocks_covered = 0
        blocks_total = 0
        for _, stats in self.function_stats.items():
            blocks_covered += stats.blocks_covered
            blocks_total += stats.blocks_total
        return len(self.function_stats), blocks_covered, blocks_total
