"""テレメトリ収集 (ingest) 層。

UDP パケットのデコード (`packet`)、欠番の追跡と受信 (`udp`)、DB への一括書き込み (`writer`) に分ける。
ライブ配信 (`location_server.live`) は DB の書き込みを待たずに、受信直後のデコード結果を受け取る。
"""
