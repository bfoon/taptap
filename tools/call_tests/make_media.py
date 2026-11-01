"""Test media made on the fly: a public-domain portrait (NASA, via scikit-image) and a fake-camera clip of it."""
import base64, io, sys
import numpy as np
from skimage import data, io as skio, transform
from common import WORK


def portrait_png():
    p = WORK / 'astronaut.png'
    if not p.exists(): skio.imsave(p, data.astronaut())
    return p


def camera_y4m():
    p = WORK / 'person.y4m'
    if p.exists(): return p
    W, H, N = 640, 360, 60
    face = (transform.resize(data.astronaut(), (360, 360), anti_aliasing=True) * 255).astype(np.uint8)
    yy, xx = np.mgrid[0:H, 0:W]; board = (((xx // 20) + (yy // 20)) % 2 * 255).astype(np.uint8)
    with open(p, 'wb') as out:
        out.write(b'YUV4MPEG2 W640 H360 F30:1 Ip A1:1 C420jpeg\n')
        for i in range(N):
            img = np.stack([board] * 3, -1).copy(); x0 = 140 + int(6 * np.sin(i / N * 2 * np.pi)); img[:, x0:x0 + 360] = face
            r, g, b = [img[..., k].astype(np.float32) for k in range(3)]
            Y = 0.299 * r + 0.587 * g + 0.114 * b; U = -0.169 * r - 0.331 * g + 0.5 * b + 128; V = 0.5 * r - 0.419 * g - 0.081 * b + 128
            sub = lambda c: c.reshape(H // 2, 2, W // 2, 2).mean(axis=(1, 3))
            out.write(b'FRAME\n'); [out.write(np.clip(q, 0, 255).astype(np.uint8).tobytes()) for q in (Y, sub(U), sub(V))]
    return p


if __name__ == '__main__':
    print(portrait_png(), camera_y4m())
