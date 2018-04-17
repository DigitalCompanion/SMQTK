"""
IQR Search sub-application module
"""

import json
import os
import os.path as osp
import random
import shutil
from six.moves import StringIO
import zipfile

import flask
import PIL.Image

from smqtk.algorithms.descriptor_generator import \
    get_descriptor_generator_impls, DFLT_DESCRIPTOR_FACTORY
from smqtk.algorithms.nn_index import get_nn_index_impls
from smqtk.algorithms.relevancy_index import get_relevancy_index_impls
from smqtk.iqr import IqrController, IqrSession
from smqtk.iqr.iqr_session import DFLT_REL_INDEX_CONFIG
from smqtk.representation import get_data_set_impls, DescriptorElementFactory
from smqtk.representation.data_element.file_element import DataFileElement
from smqtk.utils import Configurable
from smqtk.utils import SmqtkObject
from smqtk.utils import plugin
from smqtk.utils.file_utils import safe_create_dir
from smqtk.utils.preview_cache import PreviewCache
from smqtk.web.search_app.modules.file_upload import FileUploadMod
from smqtk.web.search_app.modules.static_host import StaticDirectoryHost

from smqtk.algorithms.descriptor_generator.pytorch_saliency_descriptor import PytorchSaliencyDescriptorGenerator


__author__ = 'paul.tunison@kitware.com'


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


class IqrSearch (SmqtkObject, flask.Flask, Configurable):
    """
    IQR Search Tab blueprint

    Components:
        * Data-set, from which base media data is provided
        * Descriptor generator, which provides descriptor generation services
          for user uploaded data.
        * NearestNeighborsIndex, from which descriptors are queried from user
          input data. This index should contain descriptors that were
          generated by the same descriptor generator configuration above (same
          dimensionality, etc.).
        * RelevancyIndex, which is populated by an initial query, and then
          iterated over within the same user session. A new instance and model
          is generated every time a new session is created (or new data is
          uploaded by the user).

    Assumes:
        * DescriptorElement related to a DataElement have the same UUIDs.

    """

    # TODO: User access white/black-list? See ``search_app/__init__.py``:L135

    @classmethod
    def get_default_config(cls):
        d = super(IqrSearch, cls).get_default_config()

        # Remove parent_app slot for later explicit specification.
        del d['parent_app']

        # fill in plugin configs
        d['data_set'] = plugin.make_config(get_data_set_impls())
        d['smap_set'] = plugin.make_config(get_data_set_impls())

        d['descr_generator'] = \
            plugin.make_config(get_descriptor_generator_impls())

        d['nn_index'] = plugin.make_config(get_nn_index_impls())

        ri_config = plugin.make_config(get_relevancy_index_impls())
        if d['rel_index_config']:
            ri_config.update(d['rel_index_config'])
        d['rel_index_config'] = ri_config

        df_config = DescriptorElementFactory.get_default_config()
        if d['descriptor_factory']:
            df_config.update(d['descriptor_factory'].get_config())
        d['descriptor_factory'] = df_config

        return d

    # noinspection PyMethodOverriding
    @classmethod
    def from_config(cls, config, parent_app):
        """
        Instantiate a new instance of this class given the configuration
        JSON-compliant dictionary encapsulating initialization arguments.

        :param config: JSON compliant dictionary encapsulating
            a configuration.
        :type config: dict

        :param parent_app: Parent containing flask app instance
        :type parent_app: smqtk.web.search_app.app.search_app

        :return: Constructed instance from the provided config.
        :rtype: IqrSearch

        """
        merged = cls.get_default_config()
        merged.update(config)

        # construct nested objects via configurations
        merged['data_set'] = \
            plugin.from_plugin_config(merged['data_set'],
                                      get_data_set_impls())
        merged['smap_set'] = \
            plugin.from_plugin_config(merged['smap_set'],
                                      get_data_set_impls())

        merged['descr_generator'] = \
            plugin.from_plugin_config(merged['descr_generator'],
                                      get_descriptor_generator_impls())
        merged['nn_index'] = \
            plugin.from_plugin_config(merged['nn_index'],
                                      get_nn_index_impls())

        merged['descriptor_factory'] = \
            DescriptorElementFactory.from_config(merged['descriptor_factory'])

        return cls(parent_app, **merged)

    def __init__(self, parent_app, data_set, smap_set, descr_generator, nn_index,
                 working_directory, rel_index_config=DFLT_REL_INDEX_CONFIG,
                 descriptor_factory=DFLT_DESCRIPTOR_FACTORY,
                 pos_seed_neighbors=500):
        """
        Initialize a generic IQR Search module with a single descriptor and
        indexer.

        :param name: Name of this blueprint instance
        :type name: str

        :param parent_app: Parent containing flask app instance
        :type parent_app: smqtk.web.search_app.IqrSearchDispatcher

        :param data_set: DataSet instance that references indexed data.
        :type data_set: SMQTK.representation.DataSet

        :param smap_set: DataSet instance that saliency map of references indexed data.
        :type smap_set: SMQTK.representation.DataSet

        :param descr_generator: DescriptorGenerator instance to use in IQR
            sessions for generating descriptors on new data.
        :type descr_generator:
            smqtk.algorithms.descriptor_generator.DescriptorGenerator

        :param nn_index: NearestNeighborsIndex instance for sessions to pull
            their review data sets from.
        :type nn_index: smqtk.algorithms.NearestNeighborsIndex

        :param rel_index_config: Plugin configuration for the RelevancyIndex to
            use.
        :type rel_index_config: dict

        :param working_directory: Directory in which to place working files.
            These may be considered temporary and may be removed between
            executions of this app. Retention of a work directory may speed
            things up in subsequent runs because of caching.

        :param descriptor_factory: DescriptorElementFactory for producing new
            DescriptorElement instances when data is uploaded to the server.
        :type descriptor_factory: DescriptorElementFactory

        :param url_prefix: Web address prefix for this blueprint.
        :type url_prefix: str

        :param pos_seed_neighbors: Number of neighbors to pull from the given
            ``nn_index`` for each positive exemplar when populating the working
            index, i.e. this value determines the size of the working index for
            IQR refinement. By default, we try to get 500 neighbors.

            Since there may be partial to significant overlap of near neighbors
            as a result of nn_index queries for positive exemplars, the working
            index may contain anywhere from this value's number of entries, to
            ``N*P``, where ``N`` is this value and ``P`` is the number of
            positive examples at the time of working index initialization.
        :type pos_seed_neighbors: int

        :raises ValueError: Invalid Descriptor or indexer type

        """
        super(IqrSearch, self).__init__(
            import_name=__name__,
            static_folder=os.path.join(SCRIPT_DIR, "static"),
            template_folder=os.path.join(SCRIPT_DIR, "templates"),
        )

        self._parent_app = parent_app
        self._data_set = data_set
        self._smap_set = smap_set
        self._descriptor_generator = descr_generator
        self._nn_index = nn_index
        self._rel_index_config = rel_index_config
        self._descr_elem_factory = descriptor_factory

        self._pos_seed_neighbors = int(pos_seed_neighbors)

        if isinstance(self._descriptor_generator, PytorchSaliencyDescriptorGenerator):
            self._saliency_descr_flag = True
        else:
            self._saliency_descr_flag = False

        # base directory that's transformed by the ``work_dir`` property into
        # an absolute path.
        self._working_dir = working_directory
        # Directory to put things to allow them to be statically available to
        # public users.
        self._static_data_prefix = "static/data"
        self._static_data_dir = osp.join(self.work_dir, 'static')

        # Custom static host sub-module
        self.mod_static_dir = StaticDirectoryHost('%s_static' % self.name,
                                                  self._static_data_dir,
                                                  self._static_data_prefix)
        self.register_blueprint(self.mod_static_dir)

        # Uploader Sub-Module
        self.upload_work_dir = os.path.join(self.work_dir, "uploads")
        self.mod_upload = FileUploadMod('%s_uploader' % self.name, parent_app,
                                        self.upload_work_dir,
                                        url_prefix='/uploader')
        self.register_blueprint(self.mod_upload)
        self.register_blueprint(parent_app.module_login)

        # IQR Session control and resources
        # TODO: Move session management to database/remote?
        #       Create web-specific IqrSession class that stores/gets its state
        #       directly from database.
        self._iqr_controller = IqrController()
        # Mapping of session IDs to their work directory
        #: :type: dict[collections.Hashable, str]
        self._iqr_work_dirs = {}
        # Mapping of session ID to a dictionary of the custom example data for
        # a session (uuid -> DataElement)
        #: :type: dict[collections.Hashable, dict[collections.Hashable, smqtk.representation.DataElement]]
        self._iqr_example_data = {}
        # Descriptors of example data
        #: :type: dict[collections.Hashable, dict[collections.Hashable, smqtk.representation.DescriptorElement]]
        self._iqr_example_pos_descr = {}

        # Preview Image Caching
        self._preview_cache = PreviewCache(osp.join(self._static_data_dir,
                                                    "previews"))

        # Cache mapping of written static files for data elements
        self._static_cache = {}
        self._static_cache_element = {}

        #
        # Routing
        #

        @self.route("/")
        @self._parent_app.module_login.login_required
        def index():
            # Stripping left '/' from blueprint modules in order to make sure
            # the paths are relative to our base.
            r = {
                "module_name": self.name,
                "uploader_url": self.mod_upload.url_prefix.lstrip('/'),
                "uploader_post_url": self.mod_upload.upload_post_url().lstrip('/'),
            }
            self._log.debug("Uploader URL: %s", r['uploader_url'])
            # noinspection PyUnresolvedReferences
            return flask.render_template("iqr_search_index.html", **r)

        @self.route('/iqr_session_info', methods=["GET"])
        @self._parent_app.module_login.login_required
        def iqr_session_info():
            """
            Get information about the current IRQ session
            """
            with self.get_current_iqr_session() as iqrs:
                # noinspection PyProtectedMember
                return flask.jsonify({
                    "uuid": iqrs.uuid,

                    "descriptor_type": self._descriptor_generator.name,
                    "nn_index_type": self._nn_index.name,
                    "relevancy_index_type": self._rel_index_config['type'],

                    "positive_uids":
                        tuple(d.uuid() for d in iqrs.positive_descriptors),
                    "negative_uids":
                        tuple(d.uuid() for d in iqrs.negative_descriptors),

                    # UUIDs of example positive descriptors
                    "ex_pos": tuple(self._iqr_example_pos_descr[iqrs.uuid]),
                    "ex_neg": (),  # No user negative examples supported yet

                    "initialized": iqrs.working_index.count() > 0,
                    "index_size": iqrs.working_index.count(),
                })

        @self.route('/get_iqr_state')
        @self._parent_app.module_login.login_required
        def iqr_session_state():
            """
            Get IQR session state information composed of positive and negative
            descriptor vectors.
            """
            with self.get_current_iqr_session() as iqrs:
                iqrs_uuid = str(iqrs.uuid)
                pos_elements = list(set(
                    # Pos user examples
                    [tuple(d.vector().tolist()) for d
                     in self._iqr_example_pos_descr[iqrs.uuid].values()] +
                    # Adjudicated examples
                    [tuple(d.vector().tolist()) for d
                     in iqrs.positive_descriptors],
                ))
                neg_elements = list(set(
                    # No negative user example support yet
                    # Adjudicated examples
                    [tuple(d.vector().tolist()) for d
                     in iqrs.negative_descriptors],
                ))

            z_buffer = StringIO()
            z = zipfile.ZipFile(z_buffer, 'w', zipfile.ZIP_DEFLATED)
            z.writestr(iqrs_uuid, json.dumps({
                'pos': pos_elements,
                'neg': neg_elements,
            }))
            z.close()

            z_buffer.seek(0)

            return flask.send_file(
                z_buffer,
                mimetype='application/octet-stream',
                as_attachment=True,
                attachment_filename="%s.IqrState" % iqrs_uuid,
            )

        @self.route("/check_current_iqr_session")
        @self._parent_app.module_login.login_required
        def check_current_iqr_session():
            """
            Check that the current IQR session exists and is initialized.

            :rtype: {
                    success: bool
                }
            """
            # Getting the current IQR session ensures that one has been
            # constructed for the current session.
            with self.get_current_iqr_session():
                return flask.jsonify({
                    "success": True
                })

        @self.route("/get_data_preview_image", methods=["GET"])
        @self._parent_app.module_login.login_required
        def get_ingest_item_image_rep():
            """
            Return the base64 preview image data for the data file associated
            with the give UID.
            """
            uid = flask.request.args['uid']

            info = {
                "success": True,
                "message": None,
                "shape": None,  # (width, height)
                "static_file_link": None,
                "static_preview_link": None,
                "smap_preview_link": None,
                "smap_static_file_link": None,
            }

            # Try to find a DataElement by the given UUID in our indexed data
            # or in the session's example data.
            if self._data_set.has_uuid(uid):
                #: :type: smqtk.representation.DataElement
                de = self._data_set.get_data(uid)
            else:
                with self.get_current_iqr_session() as iqrs:
                    #: :type: smqtk.representation.DataElement | None
                    de = self._iqr_example_data[iqrs.uuid].get(uid, None)

            if not de:
                info["success"] = False
                info["message"] = "UUID not part of the active data set!"
            else:
                # Preview_path should be a path within our statically hosted
                # area.
                preview_path = self._preview_cache.get_preview_image(de)
                img = PIL.Image.open(preview_path)
                info["shape"] = img.size

                if de.uuid() not in self._static_cache:
                    self._static_cache[de.uuid()] = \
                        de.write_temp(self._static_data_dir)
                    self._static_cache_element[de.uuid()] = de

                # Need to format links by transforming the generated paths to
                # something usable by webpage:
                # - make relative to the static directory, and then pre-pending
                #   the known static url to the
                info["static_preview_link"] = \
                    self._static_data_prefix + '/' + \
                    os.path.relpath(preview_path, self._static_data_dir)
                info['static_file_link'] = \
                    self._static_data_prefix + '/' + \
                    os.path.relpath(self._static_cache[de.uuid()],
                                    self._static_data_dir)

            self._log.debug('saliency_descr_flag is {}'.format(self._saliency_descr_flag))
            if self._saliency_descr_flag:
                # obtain saliency map images
                with self.get_current_iqr_session() as iqrs:
                    if iqrs.working_index.has_descriptor(uid):
                        desr = iqrs.working_index.get_descriptor(uid)

                        # for testing only: get the saliency map's UUID of the top 1 label
                        #[0] get the top 1 label
                        # sm_d = list(desr.saliency_map().items())[0]
                        # sm_uuid = sm_d[1]

                        if iqrs.target_label not in desr.saliency_map():
                            self._log.debug('desr original dict: {}'.format(desr.saliency_map()))
                            self._log.debug('generate new saliency map for label {}'.format(iqrs.target_label))
                            temp_descr = \
                                self._descriptor_generator.compute_descriptor(
                                    de, self._descr_elem_factory, topk_label_list=[int(iqrs.target_label), 0]
                                )
                            desr.update_saliency_map(temp_descr.saliency_map())

                        sm_uuid = desr.saliency_map()[iqrs.target_label]
                        sm = self._smap_set.get_data(sm_uuid)

                        # has to put before sm.write_temp(...) since it will
                        # call clean_temp, which will delete all temp file
                        # generated by sm.write_temp(...)
                        sm_path = self._preview_cache.get_preview_image(sm)

                        if sm_uuid not in self._static_cache:
                            self._static_cache[sm_uuid] = \
                                sm.write_temp(self._static_data_dir)
                            self._static_cache_element[sm_uuid] = sm

                        info["smap_preview_link"] = \
                            self._static_data_prefix + '/' + \
                            os.path.relpath(sm_path, self._static_data_dir)
                        info['smap_static_file_link'] = \
                            self._static_data_prefix + '/' + \
                            os.path.relpath(self._static_cache[sm_uuid],
                                            self._static_data_dir)

            return flask.jsonify(info)

        @self.route('/iqr_ingest_file', methods=['POST'])
        @self._parent_app.module_login.login_required
        def iqr_ingest_file():
            """
            Ingest the file with the given UID, getting the path from the
            uploader.

            :return: string of data/descriptor element's UUID
            :rtype: str

            """
            # TODO: Add status dict with a "GET" method branch for getting that
            #       status information.

            # Start the ingest of a FID when POST
            if flask.request.method == "POST":
                with self.get_current_iqr_session() as iqrs:
                    fid = flask.request.form['fid']

                    self._log.debug("[%s::%s] Getting temporary filepath from "
                                    "uploader module", iqrs.uuid, fid)
                    upload_filepath = self.mod_upload.get_path_for_id(fid)
                    self.mod_upload.clear_completed(fid)

                    self._log.debug("[%s::%s] Moving uploaded file",
                                    iqrs.uuid, fid)
                    sess_upload = osp.join(self._iqr_work_dirs[iqrs.uuid],
                                           osp.basename(upload_filepath))
                    os.rename(upload_filepath, sess_upload)
                    upload_data = DataFileElement(sess_upload)
                    uuid = upload_data.uuid()
                    self._iqr_example_data[iqrs.uuid][uuid] = upload_data

                    # Extend session ingest -- modifying
                    self._log.debug("[%s::%s] Adding new data to session "
                                    "positives", iqrs.uuid, fid)
                    # iqrs.add_positive_data(upload_data)
                    try:
                        upload_descr = \
                            self._descriptor_generator.compute_descriptor(
                                upload_data, self._descr_elem_factory
                            )
                    except ValueError as ex:
                        return "Input Error: %s" % str(ex), 400

                    self._iqr_example_pos_descr[iqrs.uuid][uuid] = upload_descr
                    self._log.debug('saliency_descr_flag is {}'.format(self._saliency_descr_flag))
                    if self._saliency_descr_flag:
                        iqrs.target_label = int(list(upload_descr.saliency_map().items())[0][0])
                        self._log.debug('target_label {}'.format(iqrs.target_label))
                    iqrs.adjudicate((upload_descr,))

                    return str(uuid)

        @self.route("/iqr_initialize", methods=["POST"])
        @self._parent_app.module_login.login_required
        def iqr_initialize():
            """
            Initialize IQR session working index based on current positive
            examples and adjudications.
            """
            with self.get_current_iqr_session() as iqrs:
                try:
                    iqrs.update_working_index(self._nn_index)
                    return flask.jsonify({
                        "success": True,
                        "message": "Completed initialization",
                    })
                except Exception as ex:
                    return flask.jsonify({
                        "success": False,
                        "message": "ERROR: (%s) %s" % (type(ex).__name__,
                                                       str(ex))
                    })

        @self.route("/get_example_adjudication", methods=["GET"])
        @self._parent_app.module_login.login_required
        def get_example_adjudication():
            """
            Get positive/negative status for a data/descriptor in our example
            set.

            :return: {
                    is_pos: <bool>,
                    is_neg: <bool>
                }

            """
            elem_uuid = flask.request.args['uid']
            with self.get_current_iqr_session() as iqrs:
                is_p = elem_uuid in self._iqr_example_pos_descr[iqrs.uuid]
                # Currently no negative example support
                is_n = False

                return flask.jsonify({
                    "is_pos": is_p,
                    "is_neg": is_n,
                })

        @self.route("/get_index_adjudication", methods=["GET"])
        @self._parent_app.module_login.login_required
        def get_index_adjudication():
            """
            Get the adjudication status of a particular data/descriptor element
            by UUID.

            This should only ever return a dict where one of the two, or
            neither, are labeled True.

            :return: {
                    is_pos: <bool>,
                    is_neg: <bool>
                }
            """
            elem_uuid = flask.request.args['uid']
            with self.get_current_iqr_session() as iqrs:
                is_p = (
                    elem_uuid in set(d.uuid() for d
                                     in iqrs.positive_descriptors)
                )
                is_n = (
                    elem_uuid in set(d.uuid() for d
                                     in iqrs.negative_descriptors)
                )

                return flask.jsonify({
                    "is_pos": is_p,
                    "is_neg": is_n,
                })

        @self.route("/adjudicate", methods=["POST", "GET"])
        @self._parent_app.module_login.login_required
        def adjudicate():
            """
            Update adjudication for this session. This should specify UUIDs of
            data/descriptor elements in our working index.

            :return: {
                    success: <bool>,
                    message: <str>
                }
            """
            if flask.request.method == "POST":
                fetch = flask.request.form
            elif flask.request.method == "GET":
                fetch = flask.request.args
            else:
                raise RuntimeError("Invalid request method '%s'"
                                   % flask.request.method)

            pos_to_add = json.loads(fetch.get('add_pos', '[]'))
            pos_to_remove = json.loads(fetch.get('remove_pos', '[]'))
            neg_to_add = json.loads(fetch.get('add_neg', '[]'))
            neg_to_remove = json.loads(fetch.get('remove_neg', '[]'))

            self._log.debug("Adjudicated Positive{+%s, -%s}, "
                            "Negative{+%s, -%s} "
                            % (pos_to_add, pos_to_remove,
                               neg_to_add, neg_to_remove))

            with self.get_current_iqr_session() as iqrs:
                iqrs.adjudicate(
                    tuple(iqrs.working_index.get_many_descriptors(pos_to_add)),
                    tuple(iqrs.working_index.get_many_descriptors(neg_to_add)),
                    tuple(iqrs.working_index.get_many_descriptors(pos_to_remove)),
                    tuple(iqrs.working_index.get_many_descriptors(neg_to_remove)),
                )
                self._log.debug("Now positive UUIDs: %s", iqrs.positive_descriptors)
                self._log.debug("Now negative UUIDs: %s", iqrs.negative_descriptors)

            return flask.jsonify({
                "success": True,
                "message": "Adjudicated Positive{+%s, -%s}, "
                           "Negative{+%s, -%s} "
                           % (pos_to_add, pos_to_remove,
                              neg_to_add, neg_to_remove)
            })

        @self.route("/iqr_refine", methods=["POST"])
        @self._parent_app.module_login.login_required
        def iqr_refine():
            """
            Classify current IQR session indexer, updating ranking for
            display.

            Fails gracefully if there are no positive[/negative] adjudications.

            """
            with self.get_current_iqr_session() as iqrs:
                try:
                    iqrs.refine()
                    return flask.jsonify({
                        "success": True,
                        "message": "Completed refinement"
                    })
                except Exception as ex:
                    return flask.jsonify({
                        "success": False,
                        "message": "ERROR: (%s) %s" % (type(ex).__name__,
                                                       str(ex))
                    })

        @self.route("/iqr_ordered_results", methods=['GET'])
        @self._parent_app.module_login.login_required
        def get_ordered_results():
            """
            Get ordered (UID, probability) pairs in between the given indices,
            [i, j). If j Is beyond the end of available results, only available
            results are returned.

            This may be empty if no refinement has yet occurred.

            Return format:
            {
                results: [ (uid, probability), ... ]
            }
            """
            with self.get_current_iqr_session() as iqrs:
                i = int(flask.request.args.get('i', 0))
                j = int(flask.request.args.get('j', len(iqrs.results)
                                               if iqrs.results else 0))
                #: :type: tuple[(smqtk.representation.DescriptorElement, float)]
                r = (iqrs.ordered_results() or ())[i:j]
                return flask.jsonify({
                    "results": [(d.uuid(), p) for d, p in r]
                })

        @self.route("/reset_iqr_session", methods=["GET"])
        @self._parent_app.module_login.login_required
        def reset_iqr_session():
            """
            Reset the current IQR session
            """
            with self.get_current_iqr_session() as iqrs:
                iqrs.reset()

                # Clearing working directory
                if os.path.isdir(self._iqr_work_dirs[iqrs.uuid]):
                    shutil.rmtree(self._iqr_work_dirs[iqrs.uuid])
                safe_create_dir(self._iqr_work_dirs[iqrs.uuid])

                # Clearing example data + descriptors
                self._iqr_example_data[iqrs.uuid].clear()
                self._iqr_example_pos_descr[iqrs.uuid].clear()

                return flask.jsonify({
                    "success": True
                })

        @self.route("/get_random_uids")
        @self._parent_app.module_login.login_required
        def get_random_uids():
            """
            Return to the client a list of working index IDs but in a random
            order. If there is currently an active IQR session with elements in
            its extension ingest, then those IDs are included in the random
            list.

            :return: {
                    uids: list of int
                }
            """
            with self.get_current_iqr_session() as iqrs:
                all_ids = list(iqrs.working_index.keys())
            random.shuffle(all_ids)
            return flask.jsonify({
                "uids": all_ids
            })

    def __del__(self):
        for wdir in self._iqr_work_dirs.values():
            if os.path.isdir(wdir):
                shutil.rmtree(wdir)

    def get_config(self):
        return {
            'name': self.name,
            'url_prefix': self.url_prefix,
            'working_directory': self._working_dir,
            'data_set': plugin.to_plugin_config(self._data_set),
            'smap_set': plugin.to_plugin_config(self._smap_set),
            'descr_generator':
                plugin.to_plugin_config(self._descriptor_generator),
            'nn_index': plugin.to_plugin_config(self._nn_index),
            'rel_index_config': self._rel_index_config,
            'descriptor_factory': self._descr_elem_factory.get_config(),
        }

    @property
    def work_dir(self):
        """
        :return: Common work directory for this instance.
        :rtype: str
        """
        return osp.expanduser(osp.abspath(self._working_dir))

    def get_current_iqr_session(self):
        """
        Get the current IQR Session instance.

        :rtype: smqtk.IQR.iqr_session.IqrSession

        """
        with self._iqr_controller:
            sid = flask.session.sid
            if not self._iqr_controller.has_session_uuid(sid):
                iqr_sess = IqrSession(self._pos_seed_neighbors,
                                      self._rel_index_config,
                                      sid)
                # iqr_sess = IqrSession(min(self._pos_seed_neighbors, 20),
                #                       self._rel_index_config,
                #                       sid)
                self._iqr_controller.add_session(iqr_sess)
                self._iqr_work_dirs[iqr_sess.uuid] = \
                    osp.join(self.work_dir, sid)
                safe_create_dir(self._iqr_work_dirs[iqr_sess.uuid])
                self._iqr_example_data[iqr_sess.uuid] = {}
                self._iqr_example_pos_descr[iqr_sess.uuid] = {}

            return self._iqr_controller.get_session(sid)
