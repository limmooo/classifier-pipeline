from datetime import datetime
import json
import logging
import os
import time

import psutil
import numpy as np
from tensorflow.keras.applications.inception_v3 import (
    preprocess_input as inc3preprocess,
)
from classify.trackprediction import Predictions
from load.clip import Clip
from load.cliptrackextractor import ClipTrackExtractor
from ml_tools.preprocess import preprocess_segment
from ml_tools.previewer import Previewer
from ml_tools import tools
from .cptvrecorder import CPTVRecorder
from .motiondetector import MotionDetector
from .processor import Processor
from ml_tools.preprocess import (
    preprocess_frame,
    preprocess_movement,
)
import logging
from ml_tools.logs import init_logging

STOP_SIGNAL = "stop"

SKIP_SIGNAL = "skip"


class NeuralInterpreter:
    def __init__(self, model_name):
        from openvino.inference_engine import IENetwork, IECore

        # device = "CPU"
        device = "MYRIAD"
        model_xml = model_name + ".xml"
        model_bin = os.path.splitext(model_xml)[0] + ".bin"
        ie = IECore()
        net = IENetwork(model=model_xml, weights=model_bin)
        self.input_blob = next(iter(net.inputs))
        self.out_blob = next(iter(net.outputs))
        net.batch_size = 1
        self.exec_net = ie.load_network(network=net, device_name=device)
        self.load_json(model_name)

    def classify_frame(self, input_x):
        if input_x is None:
            return None
        logging.info("classify with shape %s", input_x.shape)
        rearranged_arr = np.transpose(input_x, axes=[2, 0, 1])
        input_x = np.array([[rearranged_arr]])
        res = self.exec_net.infer(inputs={self.input_blob: input_x})
        res = res[self.out_blob]
        return res[0]

    def load_json(self, filename):
        """Loads model and parameters from file."""
        stats = json.load(open(filename + ".txt", "r"))

        self.MODEL_NAME = stats["name"]
        self.MODEL_DESCRIPTION = stats["description"]
        self.labels = stats["labels"]
        # self.eval_score = stats["score"]
        self.params = stats["hyperparams"]


class LiteInterpreter:
    def __init__(self, model_name):
        import tensorflow as tf

        self.interpreter = tf.lite.Interpreter(model_path=model_name + ".tflite")

        self.interpreter.allocate_tensors()
        input_details = self.interpreter.get_tensor_details()

        self.in_values = {}
        for detail in input_details:
            self.in_values[detail["name"]] = detail["index"]

        output_details = self.interpreter.get_output_details()
        self.out_values = {}
        for detail in output_details:

            self.out_values[detail["name"]] = detail["index"]

        self.load_json(model_name)
        print("out values", self.out_values)
        self.prediction = self.out_values["Identity"]

    def classify_frame(self, input_x):
        input_x = np.float32(input_x)
        input_x = input_x[np.newaxis, :]

        self.interpreter.set_tensor(self.in_values["input"], input_x)
        self.interpreter.invoke()
        pred = self.interpreter.get_tensor(self.out_values["Identity"])[0]
        return pred

    def load_json(self, filename):
        stats = json.load(open(filename + ".txt", "r"))

        self.MODEL_NAME = stats["name"]
        self.MODEL_DESCRIPTION = stats["description"]
        self.labels = stats["labels"]
        # self.eval_score = stats["score"]
        self.params = stats["hyperparams"]


def get_full_classifier(config):
    from ml_tools.kerasmodel import KerasModel

    """
    Returns a classifier object, which is created on demand.
    This means if the ClipClassifier is copied to a new process a new Classifier instance will be created.
    """
    t0 = datetime.now()
    logging.info("classifier loading")
    model = KerasModel()
    model.load_model(config.classify.model)
    # classifier = Model(
    #     train_config=config.train,
    #     session=tools.get_session(disable_gpu=not config.use_gpu),
    # )
    # classifier.load(config.classify.model)
    logging.info("classifier loaded ({})".format(datetime.now() - t0))

    return model3


def get_classifier(config):
    model_name, model_type = os.path.splitext(config.classify.model)
    if model_type == ".tflite":
        classifier = LiteInterpreter(model_name)
    elif model_type == ".xml":
        classifier = NeuralInterpreter(model_name)
    else:
        classifier = get_full_classifier(config)
    return classifier


def run(frame_queue, config, thermal_config, headers):
    init_logging()
    pi_classifier = PiClassifier(config, thermal_config, headers)
    while True:
        frame = frame_queue.get()
        if isinstance(frame, str):
            if frame == STOP_SIGNAL:
                logging.info("PiClassifier received stop signal")
                pi_classifier.disconnected()
                return
            if frame == "skip":
                pi_classifier.skip_frame()
        else:
            pi_classifier.process_frame(frame)


class PiClassifier(Processor):
    """Classifies frames from leptond"""

    NUM_CONCURRENT_TRACKS = 4
    DEBUG_EVERY = 100
    MAX_CONSEC = 1
    # after every MAX_CONSEC frames skip this many frames
    # this gives the cpu a break
    SKIP_FRAMES = 10

    def __init__(self, config, thermal_config, headers):
        self.headers = headers
        self.frame_num = 0
        self.clip = None
        self.tracking = False
        self.enable_per_track_information = False
        self.rolling_track_classify = {}
        self.skip_classifying = 0
        self.classified_consec = 0
        self.config = config
        self.classifier = get_classifier(config)

        self.num_labels = len(self.classifier.labels)
        self.process_time = 0
        self.tracking_time = 0
        self.identify_time = 0
        self.predictions = Predictions(self.classifier.labels)
        self.preview_frames = thermal_config.recorder.preview_secs * headers.fps
        edge = self.config.tracking.edge_pixels
        self.crop_rectangle = tools.Rectangle(
            edge, edge, headers.res_x - 2 * edge, headers.res_y - 2 * edge
        )

        try:
            self.fp_index = self.classifier.labels.index("false-positive")
        except ValueError:
            self.fp_index = None
        self.track_extractor = ClipTrackExtractor(
            self.config.tracking,
            self.config.use_opt_flow,
            self.config.classify.cache_to_disk,
            keep_frames=False,
            calc_stats=False,
        )
        self.motion = thermal_config.motion
        self.min_frames = thermal_config.recorder.min_secs * headers.fps
        self.max_frames = thermal_config.recorder.max_secs * headers.fps
        self.motion_detector = MotionDetector(
            thermal_config,
            self.config.tracking.motion.dynamic_thresh,
            CPTVRecorder(thermal_config, headers),
            headers,
        )
        self.startup_classifier()

        self._output_dir = thermal_config.recorder.output_dir
        self.meta_dir = os.path.join(thermal_config.recorder.output_dir)
        if not os.path.exists(self.meta_dir):
            os.makedirs(self.meta_dir)

    def new_clip(self):
        self.clip = Clip(
            self.config.tracking,
            "stream",
            tracking_version=self.track_extractor.VERSION,
        )
        self.clip.video_start_time = datetime.now()
        self.clip.num_preview_frames = self.preview_frames
        self.clip.set_res(self.res_x, self.res_y)
        self.clip.set_frame_buffer(
            self.config.classify_tracking.high_quality_optical_flow,
            self.config.classify.cache_to_disk,
            self.config.use_opt_flow,
            True,
        )
        self.clip.predictions = self.predictions
        # process preview_frames
        frames = self.motion_detector.thermal_window.get_frames()
        edge_pixels = self.config.tracking.edge_pixels

        self.clip.update_background(self.motion_detector.background)
        self.clip._background_calculated()
        for frame in frames:
            self.track_extractor.process_frame(self.clip, frame.pix.copy())

    def startup_classifier(self):
        # classifies an empty frame to force loading of the model into memory

        p_frame = np.zeros((160, 160, 3), np.float32)
        self.classifier.classify_frame(p_frame)

    def get_active_tracks(self):
        """
        Gets current clips active_tracks and returns the top NUM_CONCURRENT_TRACKS order by priority
        """
        active_tracks = self.clip.active_tracks
        if len(active_tracks) <= PiClassifier.NUM_CONCURRENT_TRACKS:
            return active_tracks
        active_predictions = []
        for track in active_tracks:
            prediction = self.predictions.get_or_create_prediction(
                track, keep_all=False
            )
            active_predictions.append(prediction)

        top_priority = sorted(
            active_predictions,
            key=lambda i: i.get_priority(self.clip.frame_on),
            reverse=True,
        )

        top_priority = [
            track.track_id
            for track in top_priority[: PiClassifier.NUM_CONCURRENT_TRACKS]
        ]
        classify_tracks = [
            track for track in active_tracks if track.get_id() in top_priority
        ]
        return classify_tracks

    def identify_last_frame(self):
        """
        Runs through track identifying segments, and then returns it's prediction of what kind of animal this is.
        One prediction will be made for every active_track of the last frame.
        :return: TrackPrediction object
        """

        prediction_smooth = 0.1

        smooth_prediction = None
        smooth_novelty = None

        prediction = 0.0
        novelty = 0.0
        active_tracks = self.get_active_tracks()

        for i, track in enumerate(active_tracks):

            regions = []
            if len(track) < 10:
                continue
            track_prediction = self.predictions.get_or_create_prediction(
                track, keep_all=False
            )
            regions = track.bounds_history[-50:]
            frames = self.clip.frame_buffer.get_last_x(len(regions))
            if frames is None:
                return
            indices = np.random.choice(
                len(regions), min(25, len(regions)), replace=False
            )
            indices.sort()
            frames = np.array(frames)[indices]
            regions = np.array(regions)[indices]

            refs = []
            for frame in frames:
                refs.append(np.median(frame.thermal))
                thermal_reference = np.median(frame.thermal)
            segment_data = []
            for i, frame in enumerate(frames):
                segment_data.append(frame.crop_by_region(regions[i]))

            preprocessed = preprocess_movement(
                None,
                segment_data,
                5,
                None,
                0,
                inc3preprocess,
                reference_level=refs,
                sample="Test-{}".format(self.clip.frame_on),
                type=1,
            )
            if preprocessed is None:
                continue
            prediction = self.classifier.classify_frame(preprocessed)
            # print("prediction is", np.round(100 * prediction))
            track_prediction.classified_frame(self.clip.frame_on, prediction, None)

    def get_recent_frame(self):
        return self.motion_detector.get_recent_frame()

    def disconnected(self):
        self.end_clip()
        self.motion_detector.disconnected()

    def skip_frame(self):
        self.skip_classifying -= 1

        if self.clip:
            self.clip.frame_on += 1

    def process_frame(self, lepton_frame):
        start = time.time()
        self.motion_detector.process_frame(lepton_frame)
        self.process_time += time.time() - start
        if self.motion_detector.recorder.recording:
            if self.clip is None:
                self.new_clip()
                self.motion_detector.recorder.clip = self.clip

            t_start = time.time()
            self.track_extractor.process_frame(
                self.clip, lepton_frame.pix, self.motion_detector.ffc_affected
            )
            self.tracking_time += time.time() - t_start
            if self.motion_detector.ffc_affected or self.clip.on_preview():
                self.skip_classifying = PiClassifier.SKIP_FRAMES
                self.classified_consec = 0
            elif (
                self.motion_detector.ffc_affected is False
                and self.clip.active_tracks
                and self.skip_classifying <= 0
                and not self.clip.on_preview()
            ):
                id_start = time.time()
                self.identify_last_frame()
                self.identify_time += time.time() - id_start
                self.classified_consec += 1
                if self.classified_consec == PiClassifier.MAX_CONSEC:
                    self.skip_classifying = PiClassifier.SKIP_FRAMES
                    self.classified_consec = 0

        elif self.clip is not None:
            self.end_clip()

        self.skip_classifying -= 1
        self.frame_num += 1
        end = time.time()
        timetaken = end - start
        if (
            self.motion_detector.can_record()
            and self.frame_num % PiClassifier.DEBUG_EVERY == 0
        ):
            logging.info(
                "tracking {} process {} rec {} identify {} fps {}/sec time to process {}ms cpu % {} memory % {}".format(
                    round(self.tracking_time, 3),
                    round(self.process_time, 3),
                    round(self.motion_detector.rec_time, 3),
                    round(self.identify_time, 3),
                    round(1 / timetaken, 2),
                    round(timetaken * 1000, 2),
                    psutil.cpu_percent(),
                    psutil.virtual_memory()[2],
                )
            )
            self.tracking_time = 0
            self.process_time = 0
            self.identify_time = 0
            self.motion_detector.rec_time = 0

    def create_mp4(self):
        previewer = Previewer(self.config, "classified")
        previewer.export_clip_preview(
            self.clip.get_id() + ".mp4", self.clip, self.predictions
        )

    def end_clip(self):
        if self.clip:
            self.track_extractor.filter_tracks(self.clip)
            for _, prediction in self.predictions.prediction_per_track.items():
                if prediction.max_score:
                    logging.info(
                        "Clip {} {}".format(
                            self.clip.get_id(),
                            prediction.description(self.predictions.labels),
                        )
                    )
            # self.save_metadata()
            # self.create_mp4()
            self.predictions.clear_predictions()
            self.clip = None
            self.tracking = False

    @property
    def res_x(self):
        return self.headers.res_x

    @property
    def res_y(self):
        return self.headers.res_y

    @property
    def output_dir(self):
        return self._output_dir
