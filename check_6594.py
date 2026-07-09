import re, socket, urllib.request
socket.setdefaulttimeout(35)
UA = "Mozilla/5.0"
def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read()
html = fetch("https://www.jpx.co.jp/markets/statistics-equities/misc/01.html").decode("utf-8", "replace")
link = re.findall(r'href="([^"]*data_j\.xlsx?[^"]*)"', html, re.I)[0]
url = "https://www.jpx.co.jp" + link
raw = fetch(url)
import xlrd
wb = xlrd.open_workbook(file_contents=raw)
ws = wb.sheet_by_index(0)
for i in range(1, ws.nrows):
    row = ws.row_values(i)
    if str(row[1]).startswith("6594"):
        print(row)
# also check nikkei225 csv for 6594
csvraw = fetch("https://indexes.nikkei.co.jp/nkave/archives/file/nikkei_stock_average_weight_jp.csv")
text = csvraw.decode("cp932")
for line in text.splitlines():
    if '"6594"' in line:
        print("NIKKEI225:", line)
print("6594 in nikkei csv text:", '"6594"' in text)
