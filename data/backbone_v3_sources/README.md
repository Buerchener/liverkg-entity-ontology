# v3 通路与 HMDB 输入

manifest.json 记录归档日期、发布版本、来源 URL 和精确 SHA-256。构建使用与清单一致的本地文件。

- Reactome 98 提供通路名称、父子层级、人类 GO 注释和 ChEBI 到通路的映射。 [Reactome 下载页面](https://reactome.org/download-data)
- KEGG BRITE br08901 和 KEGG 化合物到参考通路的链接提供层级与代谢物关联。BRITE 页面更新时间为 2026-11-09，清单也记录了归档时的 KEGG 数据库更新时间。 [KEGG API](https://www.kegg.jp/kegg/rest/) [BRITE](https://www.kegg.jp/brite/br08901)
- HMDB 使用交接包提供的 5.0 压缩 XML。把文件放在此目录，或通过 --hmdb-xml 指向原始文件。构建对解压流核验交接校验值。

归档文件和生成的图谱快照保存在本机数据目录。通路发布版本和文件摘要随代码提交。 [KEGG 学术使用说明](https://www.kegg.jp/kegg/legal.html)
