# OPPO Motion Photo Maker

一个将 JPEG 图片与 MP4 视频合成为 Android 动态照片的 Python 脚本，兼容 Google Motion Photo，并针对 OPPO/OnePlus 相册和微信动态照片识别进行了适配。

## 功能

- 自动检查本机 `ffmpeg`、`ffprobe` 和 `exiftool`
- 直接重封装 H.264/HEVC 视频，不重新编码画面（其他编码会自动转为 H.264）
- 自动写入 OPPO/OnePlus 和 Android Motion Photo 所需元数据
- 生成后自动校验文件结构及嵌入视频的完整性

## 环境要求

- Python 3.10 或更高版本
- FFmpeg（包含 `ffmpeg` 和 `ffprobe`）
- [ExifTool](https://exiftool.org)
- 输入非 H.264/HEVC 视频转码时需要 FFmpeg 支持 `libx264`

请确保以上命令已加入系统 `PATH`。脚本不依赖第三方 Python 包。

## 基本使用

默认读取当前目录下的 `1.jpg` 和 `1.mp4`：

```powershell
python .\make_oppo_motion_photo.py
```

生成文件：

```text
1_OPPOMP.jpg
```

也可指定输入和输出文件：

```powershell
python .\make_oppo_motion_photo.py `
  --image 1.jpg `
  --video 1.mp4 `
  --output 1_OPPOMP.jpg
```

查看全部参数：

```powershell
python .\make_oppo_motion_photo.py --help
```

## 常用参数

```powershell
# 允许覆盖已有输出文件
python .\make_oppo_motion_photo.py --force
```

## 注意事项

- 本脚本不会自动裁切视频。从[Android动态照片视频格式规范](https://developer.android.com/media/platform/motion-photo-format?hl=zh-cn#video-container)来看，动态照片内部的视频的宽高比并不要求严格与照片对齐，但为了保证显示效果，若照片与视频比例差异较大，仍建议先手动裁切视频后再合成。
- 写入动态照片元数据时会覆盖原图原有的 XMP 数据（并按兼容模板写入 `hdrgm:Version="1.0"` 等字段），如原图带有需要保留的 XMP 信息请先备份。
- 输出文件名保留 `MP.jpg` 后缀有助于 Android 相册识别动态照片。
- 建议在手机上分别用系统相册、Google Photos 和微信测试最终文件。
- 使用微信发送时，记得在预览页左下角勾选“实况”。
- 不同厂商、Android 版本和第三方相册对动态照片的支持仍可能存在差异。

