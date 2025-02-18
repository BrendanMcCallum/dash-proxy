#!/usr/bin/env python

# https://www.brendanlong.com/the-structure-of-an-mpeg-dash-mpd.html

import os.path

import time
import logging
import argparse
import requests
import xml.etree.ElementTree
import copy

from termcolor import colored

logging.VERBOSE = (logging.INFO + logging.DEBUG) // 2

logger = logging.getLogger("dash-proxy")

ns = {"mpd": "urn:mpeg:dash:schema:mpd:2011"}


class Formatter(logging.Formatter):
    def __init__(self, fmt=None, datefmt=None):
        super(Formatter, self).__init__(fmt, datefmt)

    def format(self, record):
        color = None
        if record.levelno == logging.ERROR:
            color = "red"
        if record.levelno == logging.INFO:
            color = "green"
        if record.levelno == logging.WARNING:
            color = "yellow"
        if color:
            return colored(record.msg, color)
        else:
            return record.msg


ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
formatter = Formatter()
ch.setFormatter(formatter)
logger.addHandler(ch)


def baseUrl(url):
    idx = url.rfind("/")
    if idx >= 0:
        return url[: idx + 1]
    else:
        return url


class RepAddr(object):
    def __init__(self, period_idx, adaptation_set_idx, representation_idx):
        self.period_idx = period_idx
        self.adaptation_set_idx = adaptation_set_idx
        self.representation_idx = representation_idx

    def __str__(self):
        return "Representation (period=%d "\
               "adaptation-set=%d "\
               "representation=%d)" % (
            self.period_idx,
            self.adaptation_set_idx,
            self.representation_idx,
        )


class MpdLocator(object):
    def __init__(self, mpd):
        self.mpd = mpd

    def base_url(self, rep_addr):
        return self.representation(rep_addr)
                   .find("mpd:BaseURL", ns)

    def representation(self, rep_addr):
        return self.adaptation_set(rep_addr)
                   .findall("mpd:Representation", ns)[
            rep_addr.representation_idx
        ]

    def segment_template(self, rep_addr):
        rep_st = self.representation(rep_addr)
                     .find("mpd:SegmentTemplate", ns)
        if rep_st is not None:
            return rep_st
        else:
            return self.adaptation_set(rep_addr)
                       .find("mpd:SegmentTemplate", ns)

    def segment_timeline(self, rep_addr):
        return self.segment_template(rep_addr)
                   .find("mpd:SegmentTimeline", ns)

    def adaptation_set(self, rep_addr):
        return self.mpd.findall("mpd:Period", ns)[rep_addr.period_idx]
                   .findall(
            "mpd:AdaptationSet", ns
        )[rep_addr.adaptation_set_idx]


class HasLogger(object):
    def verbose(self, msg):
        self.logger.log(logging.VERBOSE, msg)

    def info(self, msg):
        self.logger.log(logging.INFO, msg)

    def debug(self, msg):
        self.logger.log(logging.DEBUG, msg)

    def warning(self, msg):
        self.logger.log(logging.WARNING, msg)

    def error(self, msg):
        self.logger.log(logging.ERROR, msg)


class DashProxy(HasLogger):
    retry_interval = 10

    def __init__(self, mpd, output_dir,
                 download, headers, cookies, save_mpds=False):
        self.logger = logger

        self.mpd = mpd
        self.output_dir = output_dir
        self.download = download
        self.save_mpds = save_mpds
        self.headers = headers
        self.cookies = cookies
        self.i_refresh = 0

        self.downloaders = {}

    def run(self):
        logger.log(
            logging.INFO,
            "Running dash proxy for stream %s. Output goes in %s"
            % (self.mpd, self.output_dir),
        )
        self.refresh_mpd()

    def refresh_mpd(self, after=0):
        self.i_refresh += 1
        if after > 0:
            time.sleep(after)

        r = requests.get(self.mpd, self.headers, self.cookies)
        if r.status_code < 200 or r.status_code >= 300:
            logger.log(
                logging.WARNING,
                "Cannot GET the MPD. Server returned %s. Retrying after %ds"
                % (r.status_code, self.retry_interval),
            )
            self.refresh_mpd(after=self.retry_interval)

        xml.etree.ElementTree.register_namespace("", ns["mpd"])
        mpd = xml.etree.ElementTree.fromstring(r.text)
        self.handle_mpd(mpd)

    def get_base_url(self, mpd):
        base_url = baseUrl(self.mpd)
        location = mpd.find("mpd:Location", ns)
        if location is not None:
            base_url = baseUrl(location.text)
        baseUrlNode = mpd.find("mpd:BaseUrl", ns)
        if baseUrlNode:
            if baseUrlNode.text.startswith("http://") or
               baseUrlNode.text.startswith("https://"):
                base_url = baseUrl(baseUrlNode.text)
            else:
                base_url = base_url + baseUrlNode.text
        return base_url

    def handle_mpd(self, mpd):
        original_mpd = copy.deepcopy(mpd)

        periods = mpd.findall("mpd:Period", ns)
        logger.log(logging.INFO, "mpd=%s" % (periods,))
        logger.log(
            logging.VERBOSE,
            "Found %d periods choosing the 1st one" % (len(periods),)
        )
        period = periods[0]
        for as_idx, adaptation_set in enumerate(
            period.findall("mpd:AdaptationSet", ns)
        ):  
            for rep_idx, representation in enumerate(
                adaptation_set.findall("mpd:Representation", ns)
            ):
                self.verbose(
                    "Found representation with id %s"
                    % (representation.attrib.get("id", "UKN"),)
                )
                rep_addr = RepAddr(0, as_idx, rep_idx)
                self.ensure_downloader(mpd, rep_addr)

        self.write_output_mpd(original_mpd)

        minimum_update_period = mpd.attrib.get("minimumUpdatePeriod", "")
        if minimum_update_period:
            # TODO parse minimum_update_period
            self.refresh_mpd(after=10)
        else:
            self.info("VOD MPD. Nothing more to do. Stopping...")

    def ensure_downloader(self, mpd, rep_addr):
        if rep_addr in self.downloaders:
            self.verbose("A downloader for %s already started" % (rep_addr,))
        else:
            self.info("Starting a downloader for %s" % (rep_addr,))
            downloader = DashDownloader(self, rep_addr)
            self.downloaders[rep_addr] = downloader
            downloader.handle_mpd(mpd, self.get_base_url(mpd),
                                  self.headers, self.cookies)

    def write_output_mpd(self, mpd):
        self.info("Writing the update MPD file")
        content = xml.etree.ElementTree.tostring(mpd, encoding="utf-8")
                     .decode("utf-8")
        dest = os.path.join(self.output_dir, "manifest.mpd")

        with open(dest, "wt") as f:
            f.write(content)

        if self.save_mpds:
            dest = os.path.join(
                self.output_dir, "manifest.{}.mpd".format(self.i_refresh)
            )
            with open(dest, "wt") as f:
                f.write(content)


class DashDownloader(HasLogger):
    def __init__(self, proxy, rep_addr):
        self.logger = logger
        self.proxy = proxy
        self.rep_addr = rep_addr
        self.mpd_base_url = ""

        self.initialization_downloaded = False

    def download_single(self, rep_addr, headers, cookies):
        dest = self.mpd.base_url(self.rep_addr).text
        dest_url = self.full_url(dest)
        self.info("download_single requesting %s from %s" % (dest, dest_url))
        r = requests.get(dest_url, headers, cookies)
        if r.status_code >= 200 and r.status_code < 300:
            self.write(dest, r.content)
        else:
            self.error(
                "cannot download %s server returned %d" %
                (dest_url, r.status_code)
            )

    def handle_mpd(self, mpd, base_url, headers, cookies):
        self.mpd_base_url = base_url
        self.mpd = MpdLocator(mpd)
        rep = self.mpd.representation(self.rep_addr)
        segment_template = self.mpd.segment_template(self.rep_addr)

        if segment_template is None:
            self.download_single(self.rep_addr, headers, cookies)
            return

        segment_timeline = self.mpd.segment_timeline(self.rep_addr)

        initialization_template = segment_template.attrib
                                                  .get("initialization", "")
        if initialization_template and not self.initialization_downloaded:
            self.initialization_downloaded = True
            self.download_template(initialization_template,
                                   headers, cookies, rep)

        segments = copy.deepcopy(segment_timeline.findall("mpd:S", ns))
        for segment in segment_timeline.findall("mpd:S", ns):
            segment_timeline.remove(segment)
        idx = 0
        for segment in segments:
            duration = int(segment.attrib.get("d", "0"))
            repeat = int(segment.attrib.get("r", "0"))
            for _ in range(0, repeat + 1):
                elem = xml.etree.ElementTree.Element(
                    "{urn:mpeg:dash:schema:mpd:2011}S",
                    attrib={"d": duration, "i": idx + 1},
                )
                segment_timeline.insert(idx, elem)
                self.verbose("appending a new elem")
                idx = idx + 1

        media_template = segment_template.attrib.get("media", "")
        next_time = 0
        for segment in segment_timeline.findall("mpd:S", ns):
            current_time = int(segment.attrib.get("t", "-1"))
            if current_time == -1:
                segment.attrib["t"] = next_time
            else:
                next_time = current_time
            next_time += int(segment.attrib.get("d", "0"))
            self.download_template(media_template, rep, segment)

    def download_template(self, template, headers, cookies,
                          representation=None, segment=None):
        dest = self.render_template(template, representation, segment)
        dest_url = self.full_url(dest)
        self.info("requesting %s from %s" % (dest, dest_url))
        r = requests.get(dest_url, headers, cookies)
        if r.status_code >= 200 and r.status_code < 300:
            self.write(dest, r.content)
        else:
            self.error(
                "cannot download %s server returned %d" %
                (dest_url, r.status_code)
            )

    def render_template(self, template, representation=None, segment=None):
        template = template.replace("$RepresentationID$",
                                    "{representation_id}")
        template = template.replace("$Time$", "{time}")
        template = template.replace(
            "$Number%05d$", "{number}"
        )  # TODO printf format width: %0[width]d (ISO/IEC 23009-1:2014(E))

        args = {}
        if representation is not None:
            args["representation_id"] = representation.attrib.get("id", "")
        if segment is not None:
            args["time"] = segment.attrib.get("t", "")
            args["number"] = segment.attrib.get("i", "")

        template = template.format(**args)
        return template

    def full_url(self, dest):
        return self.mpd_base_url + dest  # TODO remove hardcoded arrd

    def write(self, dest, content):
        dest = dest.split("?")[0]
        dest = os.path.join(self.proxy.output_dir, dest)
        dest_dir = os.path.dirname(dest)
        if not os.path.exists(dest_dir):
            os.makedirs(dest_dir)
        f = open(dest, "wb")
        f.write(content)
        f.close()


def run(args):
    logger.setLevel(logging.VERBOSE if args.verbose else logging.INFO)
    proxy = DashProxy(
        mpd=args.mpd,
        output_dir=args.output,
        download=args.download,
        save_mpds=args.save_individual_mpds,
        headers=args.http_headers,
        cookies=args.http_cookies
    )
    return proxy.run()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mpd", help="Link to MPD stream")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Print various debugging information")
    parser.add_argument("-d", "--download", action="store_true",
                        help="Retain downloaded segments")
    parser.add_argument("-o", "--output", metavar="DIR", default=".",
                        help="Directory for output")
    parser.add_argument("-c", "--cookie-jar", metavar="FILE", default=".",
                        help="Directory for output")
    parser.add_argument("--http-header", metavar="KEY=VALUE", default=[],
                        action='append', type=lambda kv: kv.split("="),
                        dest='http_headers',
                        help="A header to add to each HTTP request."\
                             "Can be repeated to add multiple headers.")
    parser.add_argument("--http-cookie", metavar="KEY=VALUE", default=[],
                        action='append', type=lambda kv: kv.split("="),
                        dest='http_cookies',
                        help="A cookie to add to each HTTP request."\
                             "Can be repeated to add multiple cookies.")
    parser.add_argument("--save-individual-mpds", action="store_true",
                        help="Save refereshed MPD manifests to file")
    args = parser.parse_args()
    args.http_headers = dict(args.http_headers)
    args.http_cookies = dict(args.http_cookies)
    run(args)


if __name__ == "__main__":
    main()
