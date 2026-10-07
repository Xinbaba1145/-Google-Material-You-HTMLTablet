#!/usr/bin/env python3
"""PixelUI 本地服务：提供页面，并为内置 Chrome 提供“网页代理”，
让大多数网站可以在应用里的 iframe 中正常浏览（去掉 X-Frame-Options / CSP 的限制）。

用法:  python3 serve.py          然后打开 http://localhost:8765
"""
import gzip, http.cookiejar, http.server, ipaddress, os, re, socket, socketserver, ssl, sys, urllib.error, urllib.parse, urllib.request, zlib

PORT = int(os.environ.get("PORT", "8765"))
ROOT = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
MAX = 40 * 1024 * 1024
JAR = http.cookiejar.CookieJar()
CTX = ssl.create_default_context()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(JAR), urllib.request.HTTPSHandler(context=CTX))

INJECT = r"""<meta name="referrer" content="no-referrer"><base href="%(base)s"><script>(function(){
var P=parent,B=%(base_js)s;
function abs(u){try{return new URL(u,B).href}catch(e){return null}}
function send(m){try{P.postMessage(Object.assign({pxb:1},m),'*')}catch(e){}}
document.addEventListener('click',function(e){
  if(e.defaultPrevented)return;var a=e.target.closest&&e.target.closest('a[href]');if(!a)return;
  var h=a.getAttribute('href');if(!h||h.charAt(0)=='#'||/^(javascript|mailto|tel|data|blob):/i.test(h))return;
  var u=abs(h);if(!u)return;e.preventDefault();
  if(a.target=='_blank'||e.ctrlKey||e.metaKey||e.button==1)send({t:'open',url:u});else send({t:'nav',url:u});
},true);
document.addEventListener('submit',function(e){
  var f=e.target;if(e.defaultPrevented)return;var m=(f.getAttribute('method')||'get').toLowerCase();
  var act=abs(f.getAttribute('action')||B);if(!act)return;
  if(m=='post'){f.setAttribute('action','/__p?u='+encodeURIComponent(act));f.removeAttribute('target');return}
  e.preventDefault();var u=new URL(act),fd=new FormData(f);
  if(e.submitter&&e.submitter.name)fd.append(e.submitter.name,e.submitter.value);
  fd.forEach(function(v,k){if(typeof v=='string')u.searchParams.set(k,v)});
  send({t:'nav',url:u.href});
},true);
window.open=function(u){var x=abs(u||'');if(x)send({t:'open',url:x});return null};
['pushState','replaceState'].forEach(function(k){var o=history[k];history[k]=function(s,t,u){var r=o.apply(this,arguments);if(u)send({t:'url',url:abs(u)});return r}});
function info(){var l=document.querySelector('link[rel~=icon]');send({t:'load',url:(history.state&&0)||B,title:document.title,icon:l?abs(l.getAttribute('href')):abs('/favicon.ico')})}
addEventListener('DOMContentLoaded',function(){info();var t=document.querySelector('title');if(t)new MutationObserver(info).observe(t,{childList:true})});
addEventListener('load',info);
})();</script>"""


def is_private(host):
    try:
        for fam, _, _, _, sa in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(sa[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return True
    except Exception:
        return False
    return False


def err_page(msg, url=""):
    body = f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>无法访问此网站</title>
<style>body{{font:16px system-ui,sans-serif;margin:0;display:grid;place-items:center;min-height:100vh;background:#f6f6f8;color:#1c1b1f}}
.c{{max-width:520px;padding:32px}}h1{{font-weight:500;font-size:26px;margin:0 0 12px}}p{{color:#49454f;line-height:1.6}}code{{background:#e7e0ec;padding:2px 8px;border-radius:8px;word-break:break-all}}
@media(prefers-color-scheme:dark){{body{{background:#141218;color:#e6e0e9}}p{{color:#cac4d0}}code{{background:#2b2930}}}}</style>
<div class=c><div style="font-size:54px">😕</div><h1>无法访问此网站</h1><p>{msg}</p><p><code>{url}</code></p><p>请检查网址、网络（部分网站在当前网络环境下可能无法连接），然后重试。</p></div>"""
    return body.encode()


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=ROOT, **k)

    def log_message(self, *a):
        pass

    def end_headers(self):
        if not self.path.startswith("/__p"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_GET(self):
        self.route()

    def do_POST(self):
        self.route()

    def do_HEAD(self):
        self.route()

    def route(self):
        if self.path.startswith("/__ping"):
            b = b"pixelui-proxy"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        if self.path.startswith("/__p"):
            return self.proxy()
        if self.command == "POST":
            self.send_error(405)
            return
        return super().do_GET() if self.command == "GET" else super().do_HEAD()

    def reply(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def proxy(self):
        q = urllib.parse.urlparse(self.path)
        url = urllib.parse.parse_qs(q.query).get("u", [""])[0]
        if not re.match(r"^https?://", url, re.I):
            return self.reply(400, err_page("不支持的网址", url))
        host = urllib.parse.urlparse(url).hostname or ""
        if is_private(host):
            return self.reply(403, err_page("出于安全考虑，不允许通过代理访问本机或局域网地址", url))
        data = None
        if self.command == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(n) if n else b""
        headers = {"User-Agent": UA, "Accept": self.headers.get("Accept", "*/*"), "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                   "Accept-Encoding": "gzip, deflate"}
        if data is not None and self.headers.get("Content-Type"):
            headers["Content-Type"] = self.headers["Content-Type"]
        rng = self.headers.get("Range")
        if rng:
            headers["Range"] = rng
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=self.command if self.command != "HEAD" else "GET")
            with OPENER.open(req, timeout=20) as r:
                final = r.geturl()
                body = r.read(MAX + 1)
                code = r.status
                ctype = r.headers.get("Content-Type", "application/octet-stream")
                enc = (r.headers.get("Content-Encoding") or "").lower()
        except urllib.error.HTTPError as e:
            final, body, code = url, e.read(MAX), e.code
            ctype = e.headers.get("Content-Type", "text/html")
            enc = (e.headers.get("Content-Encoding") or "").lower()
        except Exception as e:
            return self.reply(502, err_page(f"连接失败：{type(e).__name__}", url))
        if len(body) > MAX:
            return self.reply(413, err_page("资源过大", url))
        try:
            if enc == "gzip":
                body = gzip.decompress(body)
            elif enc == "deflate":
                try:
                    body = zlib.decompress(body)
                except zlib.error:
                    body = zlib.decompress(body, -zlib.MAX_WBITS)
        except Exception:
            pass
        extra = {"Access-Control-Allow-Origin": "*"}
        if "text/html" in ctype.lower():
            body = re.sub(rb"<meta[^>]+http-equiv=[\"']?content-security-policy[^>]*>", b"", body, flags=re.I)
            inj = (INJECT % {"base": final.replace('"', "%22"), "base_js": repr(final).replace("</", "<\\/")}).encode()
            m = re.search(rb"<head[^>]*>", body, re.I) or re.search(rb"<html[^>]*>", body, re.I)
            body = body[:m.end()] + inj + body[m.end():] if m else inj + body
        self.reply(code, body, ctype, extra)


class Srv(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    srv = Srv(("127.0.0.1", PORT), H)
    print(f"PixelUI  ->  http://localhost:{PORT}   (Ctrl+C 退出)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
