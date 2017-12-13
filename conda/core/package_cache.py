# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

from functools import reduce
from logging import getLogger
from os import listdir
from os.path import basename, dirname, join
from tarfile import ReadError

from .path_actions import CacheUrlAction, ExtractPackageAction
from .. import CondaError, CondaMultiError, conda_signal_handler
from .._vendor.auxlib.collection import first
from ..base.constants import CONDA_TARBALL_EXTENSION, PACKAGE_CACHE_MAGIC_FILE
from ..base.context import context
from ..common.compat import iteritems, itervalues, odict, text_type, with_metaclass
from ..common.constants import NULL
from ..common.io import ProgressBar
from ..common.path import expand, url_to_path
from ..common.signals import signal_handler
from ..common.url import path_to_url
from ..gateways.disk.create import (create_package_cache_directory, extract_tarball,
                                    write_as_json_to_file)
from ..gateways.disk.delete import rm_rf
from ..gateways.disk.read import (compute_md5sum, isdir, isfile, islink, read_index_json,
                                  read_index_json_from_tarball, read_repodata_json)
from ..gateways.disk.test import file_path_is_writable
from ..models.dist import Dist
from ..models.index_record import PackageRecord, PackageRef
from ..models.match_spec import MatchSpec
from ..models.package_cache_record import PackageCacheRecord

try:
    from cytoolz.itertoolz import concat, concatv, groupby
except ImportError:  # pragma: no cover
    from .._vendor.toolz.itertoolz import concat, concatv, groupby  # NOQA


log = getLogger(__name__)


class PackageCacheType(type):
    """
    This metaclass does basic caching of PackageCache instance objects.
    """

    def __call__(cls, pkgs_dir):
        if isinstance(pkgs_dir, PackageCache):
            return pkgs_dir
        elif pkgs_dir in PackageCache._cache_:
            return PackageCache._cache_[pkgs_dir]
        else:
            package_cache_instance = super(PackageCacheType, cls).__call__(pkgs_dir)
            PackageCache._cache_[pkgs_dir] = package_cache_instance
            return package_cache_instance


@with_metaclass(PackageCacheType)
class PackageCache(object):
    _cache_ = {}

    def __init__(self, pkgs_dir):
        self.pkgs_dir = pkgs_dir
        self.__package_cache_records = None
        self.__is_writable = None

        self._urls_data = UrlsData(pkgs_dir)

    def insert(self, package_cache_record):

        meta = join(package_cache_record.extracted_package_dir, 'info', 'repodata_record.json')
        write_as_json_to_file(meta, PackageRecord.from_objects(package_cache_record))

        self._package_cache_records[package_cache_record] = package_cache_record

    def load(self):
        self.__package_cache_records = _package_cache_records = {}
        self._check_writable()  # called here to create the cache if it doesn't exist
        if not isdir(self.pkgs_dir):
            # no directory exists, and we didn't have permissions to create it
            return

        for base_name in self._dedupe_pkgs_dir_contents(listdir(self.pkgs_dir)):
            full_path = join(self.pkgs_dir, base_name)
            if islink(full_path):
                continue
            elif (isdir(full_path) and isfile(join(full_path, 'info', 'index.json'))
                  or isfile(full_path) and full_path.endswith(CONDA_TARBALL_EXTENSION)):
                package_cache_record = self._make_single_record(base_name)
                if package_cache_record:
                    _package_cache_records[package_cache_record] = package_cache_record

    def get(self, package_ref, default=NULL):
        assert isinstance(package_ref, PackageRef)
        try:
            return self._package_cache_records[package_ref]
        except KeyError:
            if default is not NULL:
                return default
            else:
                raise

    def remove(self, package_ref, default=NULL):
        if default is NULL:
            return self._package_cache_records.pop(package_ref)
        else:
            return self._package_cache_records.pop(package_ref, default)

    def query(self, package_ref_or_match_spec):
        # returns a generator
        param = package_ref_or_match_spec
        if isinstance(param, MatchSpec):
            return (pcrec for pcrec in itervalues(self._package_cache_records)
                    if param.match(pcrec))
        else:
            # assume isinstance(param, PackageRef)
            return (pcrec for pcrec in itervalues(self._package_cache_records) if pcrec == param)

    @classmethod
    def query_all(cls, package_ref_or_match_spec, pkgs_dirs=None):
        if pkgs_dirs is None:
            pkgs_dirs = context.pkgs_dirs

        return concat(pcache.query(package_ref_or_match_spec) for pcache in concatv(
            cls.writable_caches(pkgs_dirs),
            cls.read_only_caches(pkgs_dirs),
        ))

    # ##########################################################################################
    # these class methods reach across all package cache directories (usually context.pkgs_dirs)
    # ##########################################################################################

    @classmethod
    def first_writable(cls, pkgs_dirs=None):
        return cls.writable_caches(pkgs_dirs)[0]

    @classmethod
    def writable_caches(cls, pkgs_dirs=None):
        if pkgs_dirs is None:
            pkgs_dirs = context.pkgs_dirs
        writable_caches = tuple(filter(lambda c: c.is_writable,
                                       (cls(pd) for pd in pkgs_dirs)))
        if not writable_caches:
            # TODO: raise NoWritablePackageCacheError()
            raise CondaError("No writable package cache directories found in\n"
                             "%s" % text_type(pkgs_dirs))
        return writable_caches

    @classmethod
    def read_only_caches(cls, pkgs_dirs=None):
        if pkgs_dirs is None:
            pkgs_dirs = context.pkgs_dirs
        read_only_caches = tuple(filter(lambda c: not c.is_writable,
                                        (cls(pd) for pd in pkgs_dirs)))
        return read_only_caches

    @classmethod
    def get_all_extracted_entries(cls):
        package_caches = (cls(pd) for pd in context.pkgs_dirs)
        return tuple(pc_entry for pc_entry in concat(map(itervalues, package_caches))
                     if pc_entry.is_extracted)

    @classmethod
    def get_entry_to_link(cls, package_ref):
        pc_entry = next((pcrec for pcrec in cls.query_all(package_ref)
                         if pcrec.is_extracted),
                        None)
        if pc_entry is not None:
            return pc_entry

        # this can happen with `conda install path/to/package.tar.bz2`
        #   because dist has channel '<unknown>'
        # if ProgressiveFetchExtract did its job correctly, what we're looking for
        #   should be the matching dist_name in the first writable package cache
        # we'll search all caches for a match, but search writable caches first
        caches = concatv(cls.writable_caches(), cls.read_only_caches())
        dist_str = package_ref.dist_str().rsplit(':', 1)[-1]
        pc_entry = next((cache._scan_for_dist_no_channel(dist_str)
                         for cache in caches if cache), None)
        if pc_entry is not None:
            return pc_entry
        raise CondaError("No package '%s' found in cache directories." % Dist(package_ref))

    @classmethod
    def tarball_file_in_cache(cls, tarball_path, md5sum=None, exclude_caches=()):
        tarball_full_path, md5sum = cls._clean_tarball_path_and_get_md5sum(tarball_path, md5sum)
        pc_entry = first(cls(pkgs_dir).tarball_file_in_this_cache(tarball_full_path,
                                                                  md5sum)
                         for pkgs_dir in context.pkgs_dirs
                         if pkgs_dir not in exclude_caches)
        return pc_entry

    @classmethod
    def clear(cls):
        cls._cache_.clear()

    def tarball_file_in_this_cache(self, tarball_path, md5sum=None):
        tarball_full_path, md5sum = self._clean_tarball_path_and_get_md5sum(tarball_path,
                                                                            md5sum=md5sum)
        tarball_basename = basename(tarball_full_path)
        pc_entry = first((pc_entry for pc_entry in itervalues(self)),
                         key=lambda pce: pce.tarball_basename == tarball_basename
                                         and pce.md5 == md5sum)  # NOQA
        return pc_entry

    @property
    def _package_cache_records(self):
        # don't actually populate _package_cache_records until we need it
        return self.__package_cache_records or self.load() or self.__package_cache_records

    @property
    def is_writable(self):
        return self.__is_writable or self._check_writable()

    def _check_writable(self):
        if isdir(self.pkgs_dir):
            i_wri = file_path_is_writable(join(self.pkgs_dir, PACKAGE_CACHE_MAGIC_FILE))
        else:
            log.trace("package cache directory '%s' does not exist", self.pkgs_dir)
            i_wri = create_package_cache_directory(self.pkgs_dir)
            rm_rf(self.pkgs_dir)
        log.debug("package cache directory '%s' writable: %s", self.pkgs_dir, i_wri)
        self.__is_writable = i_wri
        return i_wri

    def _ensure_exists(self):
        if not isfile(join(self.pkgs_dir, PACKAGE_CACHE_MAGIC_FILE)):
            create_package_cache_directory(self.pkgs_dir)

    @staticmethod
    def _clean_tarball_path_and_get_md5sum(tarball_path, md5sum=None):
        if tarball_path.startswith('file:/'):
            tarball_path = url_to_path(tarball_path)
        tarball_full_path = expand(tarball_path)

        if isfile(tarball_full_path) and md5sum is None:
            md5sum = compute_md5sum(tarball_full_path)

        return tarball_full_path, md5sum

    def _scan_for_dist_no_channel(self, dist_str):
        return next((pcrec for pcrec in self._package_cache_records
                     if pcrec.dist_str().rsplit(':', 1)[-1] == dist_str),
                    None)

    def itervalues(self):
        return iter(self.values())

    def values(self):
        return self._package_cache_records.values()

    def __repr__(self):
        args = ('%s=%r' % (key, getattr(self, key)) for key in ('pkgs_dir',))
        return "%s(%s)" % (self.__class__.__name__, ', '.join(args))

    def _make_single_record(self, package_filename):
        if not package_filename.endswith(CONDA_TARBALL_EXTENSION):
            package_filename += CONDA_TARBALL_EXTENSION

        package_tarball_full_path = join(self.pkgs_dir, package_filename)
        log.trace("adding to package cache %s", package_tarball_full_path)
        extracted_package_dir = package_tarball_full_path[:-len(CONDA_TARBALL_EXTENSION)]

        # try reading info/repodata_record.json
        try:
            repodata_record = read_repodata_json(extracted_package_dir)
            package_cache_record = PackageCacheRecord.from_objects(
                repodata_record,
                package_tarball_full_path=package_tarball_full_path,
                extracted_package_dir=extracted_package_dir,
            )
            return package_cache_record
        except (IOError, OSError):
            # no info/repodata_record.json exists
            # try reading info/index.json
            try:
                index_json_record = read_index_json(extracted_package_dir)
            except (IOError, OSError):
                # info/index.json doesn't exist either
                if isdir(extracted_package_dir) and not isfile(package_tarball_full_path):
                    # We have a directory that looks like a conda package, but without
                    # (1) info/repodata_record.json or info/index.json, and (2) a conda package
                    # tarball, there's not much we can do.  We'll just ignore it.
                    return None

                try:
                    if self.is_writable:
                        if isdir(extracted_package_dir):
                            # We have a partially unpacked conda package directory. Best thing
                            # to do is remove it and try extracting.
                            rm_rf(extracted_package_dir)
                        extract_tarball(package_tarball_full_path, extracted_package_dir)
                        index_json_record = read_index_json(extracted_package_dir)
                    else:
                        index_json_record = read_index_json_from_tarball(package_tarball_full_path)
                except (EOFError, ReadError):
                    # EOFError: Compressed file ended before the end-of-stream marker was reached
                    # tarfile.ReadError: file could not be opened successfully
                    rm_rf(package_tarball_full_path)
                    return None

            if isfile(package_tarball_full_path):
                md5 = compute_md5sum(package_tarball_full_path)
            else:
                md5 = None

            url = first(self._urls_data, lambda x: basename(x) == package_filename)
            package_cache_record = PackageCacheRecord.from_objects(
                index_json_record,
                url=url,
                md5=md5,
                package_tarball_full_path=package_tarball_full_path,
                extracted_package_dir=extracted_package_dir,
            )

            # write the info/repodata_record.json file so we can short-circuit this next time
            if self.is_writable:
                repodata_record = PackageRecord.from_objects(package_cache_record)
                repodata_record_path = join(extracted_package_dir, 'info', 'repodata_record.json')
                write_as_json_to_file(repodata_record_path, repodata_record)

            return package_cache_record

    @staticmethod
    def _dedupe_pkgs_dir_contents(pkgs_dir_contents):
        # if both 'six-1.10.0-py35_0/' and 'six-1.10.0-py35_0.tar.bz2' are in pkgs_dir,
        #   only 'six-1.10.0-py35_0.tar.bz2' will be in the return contents
        if not pkgs_dir_contents:
            return []

        contents = []

        def _process(x, y):
            if x + CONDA_TARBALL_EXTENSION != y:
                contents.append(x)
            return y

        last = reduce(_process, sorted(pkgs_dir_contents))
        _process(last, contents and contents[-1] or '')
        return contents


class UrlsData(object):
    # this is a class to manage urls.txt
    # it should basically be thought of as a sequence
    # in this class I'm breaking the rule that all disk access goes through conda.gateways

    def __init__(self, pkgs_dir):
        self.pkgs_dir = pkgs_dir
        self.urls_txt_path = urls_txt_path = join(pkgs_dir, 'urls.txt')
        if isfile(urls_txt_path):
            with open(urls_txt_path, 'r') as fh:
                self._urls_data = [line.strip() for line in fh]
                self._urls_data.reverse()
        else:
            self._urls_data = []

    def __contains__(self, url):
        return url in self._urls_data

    def __iter__(self):
        return iter(self._urls_data)

    def add_url(self, url):
        with open(self.urls_txt_path, 'a') as fh:
            fh.write(url + '\n')
        self._urls_data.insert(0, url)

    def get_url(self, package_path):
        # package path can be a full path or just a basename
        #   can be either an extracted directory or tarball
        package_path = basename(package_path)
        if not package_path.endswith(CONDA_TARBALL_EXTENSION):
            package_path += CONDA_TARBALL_EXTENSION
        return first(self, lambda url: basename(url) == package_path)


# ##############################
# downloading
# ##############################

class ProgressiveFetchExtract(object):

    @staticmethod
    def make_actions_for_record(pref_or_spec):
        assert pref_or_spec is not None
        # returns a cache_action and extract_action

        # if the pref or spec has an md5 value
        # look in all caches for package cache record that is
        #   (1) already extracted, and
        #   (2) matches the md5
        # If one exists, no actions are needed.
        md5 = pref_or_spec.get('md5')
        if md5:
            extracted_pcrec = next((
                pcrec for pcrec in concat(PackageCache(pkgs_dir).query(pref_or_spec)
                                          for pkgs_dir in context.pkgs_dirs)
                if pcrec.is_extracted
            ), None)
            if extracted_pcrec:
                return None, None

        # there is no extracted dist that can work, so now we look for tarballs that
        #   aren't extracted
        # first we look in all writable caches, and if we find a match, we extract in place
        # otherwise, if we find a match in a non-writable cache, we link it to the first writable
        #   cache, and then extract
        first_writable_cache = PackageCache.first_writable()
        first_writable_cache._ensure_exists()
        pcrec_from_writable_cache = next((
            pcrec for pcrec in concat(pcache.query(pref_or_spec)
                                      for pcache in PackageCache.writable_caches())
            if pcrec.is_fetched
        ), None)
        if pcrec_from_writable_cache:
            # extract in place
            extract_axn = ExtractPackageAction(
                source_full_path=pcrec_from_writable_cache.package_tarball_full_path,
                target_pkgs_dir=dirname(pcrec_from_writable_cache.package_tarball_full_path),
                target_extracted_dirname=basename(pcrec_from_writable_cache.extracted_package_dir),
                record_or_spec=pcrec_from_writable_cache,
                md5sum=pcrec_from_writable_cache.md5,
            )
            return None, extract_axn

        pcrec_from_read_only_cache = next((
            pcrec for pcrec in concat(pcache.query(pref_or_spec)
                                      for pcache in PackageCache.read_only_caches())
            if pcrec.is_fetched
        ), None)

        if pcrec_from_read_only_cache:
            # we found a tarball, but it's in a read-only package cache
            # we need to link the tarball into the first writable package cache,
            #   and then extract
            try:
                expected_size_in_bytes = pref_or_spec.size
            except AttributeError:
                expected_size_in_bytes = None
            cache_axn = CacheUrlAction(
                url=path_to_url(pcrec_from_read_only_cache.package_tarball_full_path),
                target_pkgs_dir=first_writable_cache.pkgs_dir,
                target_package_basename=pcrec_from_read_only_cache.fn,
                md5sum=md5,
                expected_size_in_bytes=expected_size_in_bytes,
            )
            trgt_extracted_dirname = pcrec_from_read_only_cache.fn[:-len(CONDA_TARBALL_EXTENSION)]
            extract_axn = ExtractPackageAction(
                source_full_path=cache_axn.target_full_path,
                target_pkgs_dir=first_writable_cache.pkgs_dir,
                target_extracted_dirname=trgt_extracted_dirname,
                record_or_spec=pcrec_from_read_only_cache,
                md5sum=pcrec_from_read_only_cache.md5,
            )
            return cache_axn, extract_axn

        # if we got here, we couldn't find a matching package in the caches
        #   we'll have to download one; fetch and extract
        url = pref_or_spec.get('url')
        assert url
        try:
            expected_size_in_bytes = pref_or_spec.size
        except AttributeError:
            expected_size_in_bytes = None
        cache_axn = CacheUrlAction(
            url=url,
            target_pkgs_dir=first_writable_cache.pkgs_dir,
            target_package_basename=pref_or_spec.fn,
            md5sum=md5,
            expected_size_in_bytes=expected_size_in_bytes,
        )
        extract_axn = ExtractPackageAction(
            source_full_path=cache_axn.target_full_path,
            target_pkgs_dir=first_writable_cache.pkgs_dir,
            target_extracted_dirname=pref_or_spec.fn[:-len(CONDA_TARBALL_EXTENSION)],
            record_or_spec=pref_or_spec,
            md5sum=md5,
        )
        return cache_axn, extract_axn

    def __init__(self, link_prefs):
        """
        Args:
            link_prefs (Tuple[PackageRef]):
                A sequence of :class:`PackageRef`s to ensure available in a known
                package cache, typically for a follow-on :class:`UnlinkLinkTransaction`.
                Here, "available" means the package tarball is both downloaded and extracted
                to a package directory.
        """
        self.link_precs = link_prefs

        log.debug("instantiating ProgressiveFetchExtract with\n"
                  "  %s\n", '\n  '.join(pkg_rec.dist_str() for pkg_rec in link_prefs))

        self.paired_actions = odict()  # Map[pref, Tuple(CacheUrlAction, ExtractPackageAction)]

        self._prepared = False

    def prepare(self):
        if self._prepared:
            return

        self.paired_actions.update((prec, self.make_actions_for_record(prec))
                                   for prec in self.link_precs)
        self._prepared = True

    @property
    def cache_actions(self):
        return tuple(axns[0] for axns in itervalues(self.paired_actions) if axns[0])

    @property
    def extract_actions(self):
        return tuple(axns[1] for axns in itervalues(self.paired_actions) if axns[1])

    def execute(self):
        if not self._prepared:
            self.prepare()

        assert not context.dry_run

        if not self.cache_actions or not self.extract_actions:
            return

        if not context.verbosity and not context.quiet and not context.json:
            # TODO: use logger
            print("\nDownloading and Extracting Packages")
        else:
            log.debug("prepared package cache actions:\n"
                      "  cache_actions:\n"
                      "    %s\n"
                      "  extract_actions:\n"
                      "    %s\n",
                      '\n    '.join(text_type(ca) for ca in self.cache_actions),
                      '\n    '.join(text_type(ea) for ea in self.extract_actions))

        exceptions = []
        with signal_handler(conda_signal_handler):
            for prec_or_spec, prec_actions in iteritems(self.paired_actions):
                exc = self._execute_actions(prec_or_spec, prec_actions)
                if exc:
                    log.debug('%r', exc, exc_info=True)
                    exceptions.append(exc)

        if exceptions:
            raise CondaMultiError(exceptions)

    @staticmethod
    def _execute_actions(prec_or_spec, actions):
        cache_axn, extract_axn = actions
        if cache_axn is None and extract_axn is None:
            return

        desc = "%s %s" % (prec_or_spec.name, prec_or_spec.version)
        progress_bar = ProgressBar(desc, not context.verbosity and not context.quiet, context.json)

        download_total = 0.75  # fraction of progress for download; the rest goes to extract
        try:
            if cache_axn:
                cache_axn.verify()

                if not cache_axn.url.startswith('file:/'):
                    def progress_update_cache_axn(pct_completed):
                        progress_bar.update_to(pct_completed * download_total)
                else:
                    download_total = 0
                    progress_update_cache_axn = None

                cache_axn.execute(None)

            if extract_axn:
                extract_axn.verify()

                def progress_update_extract_axn(pct_completed):
                    progress_bar.update_to((1 - download_total) * pct_completed + download_total)

                extract_axn.execute(None)

        except Exception as e:
            if extract_axn:
                extract_axn.reverse()
            if cache_axn:
                cache_axn.reverse()
            return e
        else:
            if cache_axn:
                cache_axn.cleanup()
            if extract_axn:
                extract_axn.cleanup()
            progress_bar.finish()
        finally:
            progress_bar.close()

    def __hash__(self):
        return hash(self.link_precs)

    def __eq__(self, other):
        return hash(self) == hash(other)


# ##############################
# backward compatibility
# ##############################

def rm_fetched(dist):
    """
    Checks to see if the requested package is in the cache; and if so, it removes both
    the package itself and its extracted contents.
    """
    # in conda/exports.py and conda_build/conda_interface.py, but not actually
    #   used in conda-build
    raise NotImplementedError()


def download(url, dst_path, session=None, md5=None, urlstxt=False, retries=3):
    from ..gateways.connection.download import download as gateway_download
    gateway_download(url, dst_path, md5)


class package_cache(object):

    def __contains__(self, dist):
        return bool(PackageCache.first_writable().get(Dist(dist).to_package_ref(), None))

    def keys(self):
        return (Dist(v) for v in itervalues(PackageCache.first_writable()))

    def __delitem__(self, dist):
        PackageCache.first_writable().remove(Dist(dist).to_package_ref())
