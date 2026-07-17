import json
import base64
import numpy as np
import potrace
import cv2
import os
import sys
import traceback
import webbrowser
from threading import Timer
from flask import Flask, request, render_template
from flask_cors import CORS

def get_resource_path(relative_path):
    """ 获取 PyInstaller 打包后的资源绝对路径，兼容开发与打包环境 """
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)

app = Flask(__name__, template_folder=get_resource_path('frontend'))
CORS(app)
PORT = int(os.environ.get('PORT', '5000'))
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024

# 缓存用户上传的原始图片（OpenCV BGR 矩阵）
UPLOADED_IMAGE = None
ORIGINAL_IMAGE = None
CUTOUT_CANDIDATE = None

# 默认参数
DEFAULT_CANNY_LOW = 30
DEFAULT_CANNY_HIGH = 200
DEFAULT_TURDSIZE = 2
DEFAULT_ALPHAMAX = 1.0
DEFAULT_OPTTOLERANCE = 0.2

# 默认画笔颜色
COLOUR = '#2464b4'

# Desmos 在表达式数量过大时会显著卡顿甚至让标签页崩溃。这里同时限制
# 输入像素和输出公式数量，而不是只压缩 JPEG 文件体积。
MAX_INPUT_PIXELS = 1_200_000
MAX_INPUT_SIDE = 1400
MAX_EXPRESSIONS = 5000
MIN_PROCESS_SIDE = 320


def resize_for_processing(image, scale=1.0):
    """按像素预算缩小图片；绝不为了处理而放大。"""
    height, width = image.shape[:2]
    pixel_scale = (MAX_INPUT_PIXELS / float(width * height)) ** 0.5
    side_scale = MAX_INPUT_SIDE / float(max(width, height))
    final_scale = min(1.0, pixel_scale, side_scale) * scale
    if final_scale >= 0.999:
        return image.copy()
    new_width = max(1, int(round(width * final_scale)))
    new_height = max(1, int(round(height * final_scale)))
    return cv2.resize(image, (new_width, new_height), interpolation=cv2.INTER_AREA)


def create_cutout_candidate(image):
    """使用 GrabCut 在本地提取居中的主要人物/建筑，并生成透明预览。"""
    working = resize_for_processing(image)
    height, width = working.shape[:2]
    if min(height, width) < 20:
        raise ValueError('图片尺寸太小，无法抠图')

    mask = np.zeros((height, width), np.uint8)
    margin_x = max(2, int(width * 0.04))
    margin_y = max(2, int(height * 0.04))
    rect = (margin_x, margin_y, width - margin_x * 2, height - margin_y * 2)
    background_model = np.zeros((1, 65), np.float64)
    foreground_model = np.zeros((1, 65), np.float64)
    cv2.grabCut(
        working, mask, rect, background_model, foreground_model,
        5, cv2.GC_INIT_WITH_RECT
    )
    foreground_mask = np.where(
        (mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0
    ).astype('uint8')

    # 去掉孤立噪点并柔化边缘，预览更自然。
    kernel = np.ones((3, 3), np.uint8)
    foreground_mask = cv2.morphologyEx(foreground_mask, cv2.MORPH_OPEN, kernel)
    foreground_mask = cv2.morphologyEx(foreground_mask, cv2.MORPH_CLOSE, kernel)
    foreground_mask = cv2.GaussianBlur(foreground_mask, (5, 5), 0)

    coverage = cv2.countNonZero(foreground_mask) / float(width * height)
    if coverage < 0.01 or coverage > 0.98:
        raise ValueError('没有识别到明确主体，请换一张主体更清晰的图片')

    # 公式处理使用白底图，避免透明边缘被 OpenCV 解码丢失。
    alpha = foreground_mask.astype(np.float32) / 255.0
    white = np.full_like(working, 255)
    flattened = (
        working.astype(np.float32) * alpha[..., None]
        + white.astype(np.float32) * (1.0 - alpha[..., None])
    ).astype(np.uint8)

    bgra = cv2.cvtColor(working, cv2.COLOR_BGR2BGRA)
    bgra[:, :, 3] = foreground_mask
    ok, encoded = cv2.imencode('.png', bgra)
    if not ok:
        raise ValueError('抠图预览生成失败')
    preview = 'data:image/png;base64,' + base64.b64encode(encoded).decode('ascii')
    return flattened, preview, round(coverage * 100, 1)


def trace_image(image, turdsize, alphamax, opttolerance, canny_low, canny_high,
                max_expressions=MAX_EXPRESSIONS):
    """
    根据给定参数对输入图片进行 Canny 边缘提取 and Potrace 贝塞尔曲线拟合，并转换为 Desmos LaTeX 公式。
    """
    # 1. 转换为灰度图并提取 Canny 边缘
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    # 轻度保边去噪可压掉照片纹理和 JPEG 方块，同时保留人物/建筑主轮廓。
    gray = cv2.bilateralFilter(gray, 5, 35, 35)
    edged = cv2.Canny(gray, canny_low, canny_high)

    # 2. 垂直翻转以匹配 Desmos 的直角坐标系（图像 y 轴向下，Desmos y 轴向上）
    data = edged[::-1]

    # 3. 归一化为 0 和 1 的二值图供 Potrace 使用
    data[data > 1] = 1
    bmp = potrace.Bitmap(data)

    # 4. Potrace 曲线追踪拟合
    # turdsize: 噪点抑制大小
    # alphamax: 转折平滑度 (0.0 表示折线，1.0~1.3 表示平滑曲线)
    # opttolerance: 数值优化容差 (越小拟合越贴合原边缘)
    path = bmp.trace(
        turdsize=turdsize,
        turnpolicy=potrace.TURNPOLICY_MINORITY,
        alphamax=alphamax,
        opticurve=1,
        opttolerance=opttolerance
    )

    # 5. 生成公式列表
    latex = []
    exprid = 0

    for curve in path.curves:
        segments = curve.segments
        start = curve.start_point
        for segment in segments:
            if exprid >= max_expressions:
                return latex, image.shape[1], image.shape[0], True
            x0, y0 = start
            if segment.is_corner:
                x1, y1 = segment.c
                x2, y2 = segment.end_point
                # 折角处生成两条直线公式
                latex.append({
                    'id': f'expr-{exprid + 1}',
                    'latex': f'((1-t)*{x0:.3f}+t*{x1:.3f},(1-t)*{y0:.3f}+t*{y1:.3f})',
                    'color': COLOUR
                })
                exprid += 1
                if exprid >= max_expressions:
                    return latex, image.shape[1], image.shape[0], True
                latex.append({
                    'id': f'expr-{exprid + 1}',
                    'latex': f'((1-t)*{x1:.3f}+t*{x2:.3f},(1-t)*{y1:.3f}+t*{y2:.3f})',
                    'color': COLOUR
                })
                exprid += 1
            else:
                x1, y1 = segment.c1
                x2, y2 = segment.c2
                x3, y3 = segment.end_point
                # 贝塞尔三次曲线公式
                formula = (
                    f'((1-t)*((1-t)*((1-t)*{x0:.3f}+t*{x1:.3f})+t*((1-t)*{x1:.3f}+t*{x2:.3f}))+t*((1-t)*((1-t)*{x1:.3f}+t*{x2:.3f})+t*((1-t)*{x2:.3f}+t*{x3:.3f})),'
                    f'(1-t)*((1-t)*((1-t)*{y0:.3f}+t*{y1:.3f})+t*((1-t)*{y1:.3f}+t*{y2:.3f}))+t*((1-t)*((1-t)*{y1:.3f}+t*{y2:.3f})+t*((1-t)*{y2:.3f}+t*{y3:.3f})))'
                )
                latex.append({
                    'id': f'expr-{exprid + 1}',
                    'latex': formula,
                    'color': COLOUR
                })
                exprid += 1
            start = segment.end_point

    height, width = image.shape[0], image.shape[1]
    return latex, width, height, False


def process_image_to_latex(image, turdsize, alphamax, opttolerance, canny_low, canny_high):
    """在 Desmos 公式预算内自适应降低复杂度，避免复杂图片压垮页面。"""
    original_height, original_width = image.shape[:2]
    attempts = [
        # scale, extra noise removal, tolerance multiplier
        (1.00, 0, 1.0),
        (0.82, 2, 1.5),
        (0.68, 5, 2.2),
        (0.54, 9, 3.2),
    ]
    last_result = None
    for attempt_index, (scale, extra_turd, tolerance_multiplier) in enumerate(attempts):
        working = resize_for_processing(image, scale)
        if min(working.shape[:2]) < MIN_PROCESS_SIDE and attempt_index > 0:
            break
        result = trace_image(
            working,
            turdsize=max(turdsize, extra_turd),
            alphamax=alphamax,
            opttolerance=min(1.0, opttolerance * tolerance_multiplier),
            canny_low=canny_low,
            canny_high=canny_high,
        )
        last_result = (result, attempt_index)
        if not result[3]:
            break

    (latex, width, height, limited), attempt_index = last_result
    return latex, width, height, {
        'original_width': original_width,
        'original_height': original_height,
        'optimized': width != original_width or height != original_height or attempt_index > 0,
        'complexity_reduced': attempt_index > 0 or limited,
        'expression_limit': MAX_EXPRESSIONS,
        'truncated': limited,
    }


@app.route('/upload', methods=['POST'])
def upload():
    """
    接收用户上传的图片文件并缓存在内存中。使用默认参数进行首次提取。
    """
    global UPLOADED_IMAGE, ORIGINAL_IMAGE, CUTOUT_CANDIDATE
    file = request.files.get('image')
    if not file:
        return {'error': 'No file uploaded'}, 400

    try:
        # 将二进制文件流转换为 opencv 图像
        file_bytes = np.frombuffer(file.read(), np.uint8)
        image = cv2.imdecode(file_bytes, cv2.IMREAD_COLOR)
        if image is None:
            return {'error': 'Invalid image file'}, 400

        ORIGINAL_IMAGE = image
        UPLOADED_IMAGE = image
        CUTOUT_CANDIDATE = None
        
        # 使用默认参数计算贝塞尔曲线
        latex_list, width, height, processing = process_image_to_latex(
            image,
            turdsize=DEFAULT_TURDSIZE,
            alphamax=DEFAULT_ALPHAMAX,
            opttolerance=DEFAULT_OPTTOLERANCE,
            canny_low=DEFAULT_CANNY_LOW,
            canny_high=DEFAULT_CANNY_HIGH
        )

        return {
            'result': latex_list,
            'width': width,
            'height': height,
            'processing': processing
        }
    except Exception as e:
        traceback.print_exc()
        return {'error': str(e)}, 500


@app.route('/cutout', methods=['POST'])
def cutout():
    """生成抠图候选预览，但不替换当前用于公式化的图片。"""
    global ORIGINAL_IMAGE, CUTOUT_CANDIDATE
    if ORIGINAL_IMAGE is None:
        return {'error': '请先上传图片'}, 400
    try:
        candidate, preview, coverage = create_cutout_candidate(ORIGINAL_IMAGE)
        CUTOUT_CANDIDATE = candidate
        return {'preview': preview, 'coverage': coverage}
    except Exception as e:
        traceback.print_exc()
        return {'error': str(e)}, 422


@app.route('/cutout/confirm', methods=['POST'])
def confirm_cutout():
    """用户确认后才采用抠图结果，并立即重新生成公式。"""
    global UPLOADED_IMAGE, CUTOUT_CANDIDATE
    if CUTOUT_CANDIDATE is None:
        return {'error': '没有待确认的抠图结果'}, 400
    try:
        UPLOADED_IMAGE = CUTOUT_CANDIDATE
        CUTOUT_CANDIDATE = None
        latex_list, width, height, processing = process_image_to_latex(
            UPLOADED_IMAGE,
            turdsize=DEFAULT_TURDSIZE,
            alphamax=DEFAULT_ALPHAMAX,
            opttolerance=DEFAULT_OPTTOLERANCE,
            canny_low=DEFAULT_CANNY_LOW,
            canny_high=DEFAULT_CANNY_HIGH
        )
        return {
            'result': latex_list,
            'width': width,
            'height': height,
            'processing': processing
        }
    except Exception as e:
        traceback.print_exc()
        return {'error': str(e)}, 500


@app.route('/process', methods=['POST'])
def process():
    """
    接收最新滑块参数，对已上传的图片重新做边缘检测和曲线追踪。
    """
    global UPLOADED_IMAGE
    if UPLOADED_IMAGE is None:
        return {'error': 'No image uploaded yet. Please upload an image first.'}, 400

    try:
        data = request.json or {}
        turdsize = int(data.get('turdsize', DEFAULT_TURDSIZE))
        alphamax = float(data.get('alphamax', DEFAULT_ALPHAMAX))
        opttolerance = float(data.get('opttolerance', DEFAULT_OPTTOLERANCE))
        canny_low = int(data.get('canny_low', DEFAULT_CANNY_LOW))
        canny_high = int(data.get('canny_high', DEFAULT_CANNY_HIGH))

        latex_list, width, height, processing = process_image_to_latex(
            UPLOADED_IMAGE,
            turdsize=turdsize,
            alphamax=alphamax,
            opttolerance=opttolerance,
            canny_low=canny_low,
            canny_high=canny_high
        )

        return {
            'result': latex_list,
            'width': width,
            'height': height,
            'processing': processing
        }
    except Exception as e:
        traceback.print_exc()
        return {'error': str(e)}, 500


@app.route("/calculator")
def client():
    """
    渲染前端界面，传递 Desmos 开发者 API key。
    """
    return render_template('index.html', api_key='dcb31709b452b1cf9dc26972add0fda6')


if __name__ == '__main__':
    # 自动在浏览器中打开主页
    def open_browser():
        webbrowser.open(f'http://127.0.0.1:{PORT}/calculator')
    Timer(1, open_browser).start()

    app.run(host='0.0.0.0', port=PORT)
