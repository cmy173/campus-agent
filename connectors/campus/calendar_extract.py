"""Bounded, local-only OCR worker for the school's public calendar PDF.

This does not add OCR to student uploads and never calls an external model.
"""
import asyncio
import io
import json
from pathlib import Path
import sys


async def recognize(image):
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.storage.streams import InMemoryRandomAccessStream, DataWriter
    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise RuntimeError('OCR_UNAVAILABLE')
    data = io.BytesIO()
    image.save(data, format='PNG')
    with InMemoryRandomAccessStream() as stream:
        with DataWriter(stream) as writer:
            writer.write_bytes(data.getvalue())
            await writer.store_async()
            writer.detach_stream()
        stream.seek(0)
        decoder = await BitmapDecoder.create_async(stream)
        with await decoder.get_software_bitmap_async() as bitmap:
            result = await engine.recognize_async(bitmap)
            return result.text


async def extract(path):
    import pypdfium2 as pdfium
    output = []
    with pdfium.PdfDocument(path) as document:
        if not 1 <= len(document) <= 4:
            raise ValueError('PDF_TOO_LARGE')
        for page in document:
            width, height = page.get_size()
            if not (width > height and width/height < 2):
                raise ValueError('UNKNOWN_CALENDAR_LAYOUT')
            bitmap = page.render(scale=3000/width)
            try:
                image = bitmap.to_pil()
                w, h = image.size
                notes = image.crop((0, int(h*.50), int(w*.55), int(h*.92)))
                times = image.crop((int(w*.85), int(h*.54), int(w*.96), int(h*.93)))
                output.append({'notes': await recognize(notes), 'times': await recognize(times)})
            finally:
                bitmap.close()
                page.close()
    return output


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        print(json.dumps(asyncio.run(extract(str(Path(sys.argv[1]).resolve()))), ensure_ascii=False))
    except Exception:
        print('{"error":"CALENDAR_OCR_UNAVAILABLE"}')
        sys.exit(1)
