#!/usr/bin/python3

import argparse
import base64
import hashlib
import json
import os
import ssl
import time

from urllib.parse import parse_qs
import http.server as http_server

repositories = {}
icons = {}


def get_index():
    results = []
    for repo_name in sorted(repositories.keys()):
        repo = repositories[repo_name]
        results.append(
            {
                "Name": repo_name,
                "Images": repo["images"],
                "Lists": [],
            }
        )

    return json.dumps({"Registry": "/", "Results": results}, indent=4).encode("UTF-8")


def cache_icon(data_uri):
    prefix = "data:image/png;base64,"
    assert data_uri.startswith(prefix)
    data = base64.b64decode(data_uri[len(prefix) :])
    h = hashlib.sha256()
    h.update(data)
    digest = h.hexdigest()
    filename = digest + ".png"
    icons[filename] = data

    return "/icons/" + filename


serial = 0
server_start_time = int(time.time())


def get_etag():
    return str(server_start_time) + "-" + str(serial)


def modified():
    global serial
    serial += 1


class RequestHandler(http_server.BaseHTTPRequestHandler):
    def check_route(self, route):
        parts = self.path.split("?", 1)
        path = parts[0].split("/")

        route_path = route.split("/")
        if len(route_path) != len(path):
            return False

        matches = {}
        for i in range(1, len(route_path)):
            if route_path[i][0] == "@":
                matches[route_path[i][1:]] = path[i]
            elif route_path[i] != path[i]:
                return False

        self.matches = matches
        if len(parts) == 1:
            self.query = {}
        else:
            self.query = parse_qs(parts[1], keep_blank_values=True)

        return True

    def do_GET(self):
        response = 404
        response_string = b""
        response_content_type = "application/octet-stream"

        add_headers = {}

        def get_file_contents(repo_name, type, ref):
            try:
                path = repositories[repo_name][type][ref]
                with open(path, "rb") as f:
                    return 200, f.read()
            except KeyError:
                return 404, b""

        if self.check_route("/v2/@repo_name/blobs/@digest"):
            repo_name = self.matches["repo_name"]
            digest = self.matches["digest"]
            response, response_string = get_file_contents(repo_name, "blobs", digest)
        elif self.check_route("/v2/@repo_name/manifests/@ref"):
            repo_name = self.matches["repo_name"]
            ref = self.matches["ref"]
            response, response_string = get_file_contents(repo_name, "manifests", ref)
        elif self.check_route("/index/static") or self.check_route("/index/dynamic"):
            etag = get_etag()
            add_headers["Etag"] = etag
            if self.headers.get("If-None-Match") == etag:
                response = 304
                response_string = b""
            else:
                response = 200
                response_string = get_index()
        elif self.check_route("/icons/@filename"):
            response = 200
            response_string = icons[self.matches["filename"]]
            response_content_type = "image/png"

        assert isinstance(response, int)
        assert isinstance(response_string, bytes)
        assert isinstance(response_content_type, str)

        add_headers["Content-Length"] = len(response_string)

        self.send_response(response)
        for k, v in list(add_headers.items()):
            self.send_header(k, v)

        if response == 200:
            self.send_header("Content-Type", response_content_type)

        if response == 200 or response == 304:
            self.send_header("Cache-Control", "no-cache")

        self.end_headers()

        self.wfile.write(response_string)

    def do_HEAD(self):
        return self.do_GET()

    def do_POST(self):
        if self.check_route("/testing/@repo_name/@tag"):
            repo_name = self.matches["repo_name"]
            tag = self.matches["tag"]
            d = self.query["d"][0]
            detach_icons = "detach-icons" in self.query

            repo = repositories.setdefault(repo_name, {})
            blobs = repo.setdefault("blobs", {})
            manifests = repo.setdefault("manifests", {})
            images = repo.setdefault("images", [])

            with open(os.path.join(d, "index.json")) as f:
                index = json.load(f)

            manifest_digest = index["manifests"][0]["digest"]
            manifest_path = os.path.join(d, "blobs", *manifest_digest.split(":"))
            manifests[manifest_digest] = manifest_path
            manifests[tag] = manifest_path

            with open(manifest_path) as f:
                manifest = json.load(f)

            config_digest = manifest["config"]["digest"]
            config_path = os.path.join(d, "blobs", *config_digest.split(":"))

            with open(config_path) as f:
                config = json.load(f)

            for dig in os.listdir(os.path.join(d, "blobs", "sha256")):
                digest = "sha256:" + dig
                path = os.path.join(d, "blobs", "sha256", dig)
                if digest != manifest_digest:
                    blobs[digest] = path

            if detach_icons:
                for size in (64, 128):
                    annotation = "org.freedesktop.appstream.icon-{}".format(size)
                    icon = manifest.get("annotations", {}).get(annotation)
                    if icon:
                        path = cache_icon(icon)
                        manifest["annotations"][annotation] = path
                    else:
                        icon = (
                            config.get("config", {}).get("Labels", {}).get(annotation)
                        )
                        if icon:
                            path = cache_icon(icon)
                            config["config"]["Labels"][annotation] = path

            image = {
                "Tags": [tag],
                "Digest": manifest_digest,
                "MediaType": "application/vnd.oci.image.manifest.v1+json",
                "OS": config["os"],
                "Architecture": config["architecture"],
                "Annotations": manifest.get("annotations", {}),
                "Labels": config.get("config", {}).get("Labels", {}),
            }

            # Delete old versions
            for i in images:
                if tag in i["Tags"]:
                    images.remove(i)
                    del manifests[i["Digest"]]

            images.append(image)

            modified()
            self.send_response(200)
            self.end_headers()
            return
        else:
            self.send_response(404)
            self.end_headers()
            return

    def do_DELETE(self):
        if self.check_route("/testing/@repo_name/@ref"):
            repo_name = self.matches["repo_name"]
            ref = self.matches["ref"]

            repo = repositories.setdefault(repo_name, {})
            repo.setdefault("blobs", {})
            manifests = repo.setdefault("manifests", {})
            images = repo.setdefault("images", [])

            image = None
            for i in images:
                if i["Digest"] == ref or ref in i["Tags"]:
                    image = i
                    break

            assert image

            images.remove(image)
            del manifests[image["Digest"]]
            for t in image["Tags"]:
                del manifests[t]

            modified()
            self.send_response(200)
            self.end_headers()
            return
        else:
            self.send_response(404)
            self.end_headers()
            return


def run(args):
    RequestHandler.protocol_version = "HTTP/1.0"
    httpd = http_server.HTTPServer(("127.0.0.1", 0), RequestHandler)

    if args.cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.options |= ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
        context.load_cert_chain(certfile=args.cert, keyfile=args.key)

        if args.mtls_cacert:
            context.load_verify_locations(cafile=args.mtls_cacert)
            # In a real application, we'd need to check the CN against authorized users
            context.verify_mode = ssl.CERT_REQUIRED

        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)

    host, port = httpd.socket.getsockname()[:2]
    with open("httpd-port", "w") as file:
        file.write("%d" % port)
    try:
        os.write(3, bytes("Started\n", "utf-8"))
    except OSError:
        pass
    if args.cert:
        print("Serving HTTPS on port %d" % port)
    else:
        print("Serving HTTP on port %d" % port)
    if args.dir:
        os.chdir(args.dir)
    httpd.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir")
    parser.add_argument("--cert")
    parser.add_argument("--key")
    parser.add_argument("--mtls-cacert")
    args = parser.parse_args()

    run(args)
