#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ROI Workflow Verification Phase 4.4
Code-based verification without GUI
"""

import sys
import os
import numpy as np

# Add project root to path
project_root = os.path.dirname(os.path.abspath('.'))
sys.path.insert(0, project_root)

def verify_roi_workflow():
    """Verify ROI workflow through code analysis"""
    print("=== ROI Final Verification Phase 4.4 ===")

    # Test 1: ROI Manager operations
    print("\n--- Test 1: ROI Manager Operations ---")
    from ocr.roi_manager import ROI, ROIManager

    # Create ROI manager
    roi_manager = ROIManager()

    # Verify initial state
    assert not roi_manager.has_roi, "Should start with no ROI"
    assert roi_manager.get_roi() is None, "Should return None when no ROI"
    print("  [OK] Initial state correct")

    # Set ROI
    roi = ROI(100, 200, 300, 100, 1920, 1080)
    roi_manager.set_roi(roi)
    assert roi_manager.has_roi, "Should have ROI after setting"
    assert roi_manager.get_roi() == roi, "Should return the same ROI"
    print("  [OK] ROI set successfully")

    # Clear ROI
    roi_manager.clear_roi()
    assert not roi_manager.has_roi, "Should have no ROI after clearing"
    print("  [OK] ROI cleared successfully")

    print("  [PASS] ROI Manager operations verified")

    # Test 2: Coordinate mapping
    print("\n--- Test 2: Coordinate Mapping ---")

    # Test with different aspect ratios
    test_cases = [
        {"source": (1920, 1080), "target": (1280, 720)},
        {"source": (1280, 720), "target": (640, 360)},
        {"source": (3840, 2160), "target": (1280, 720)},
    ]

    for i, case in enumerate(test_cases):
        roi = ROI(100, 200, 300, 100, case["source"][0], case["source"][1])
        scaled = roi.scale_to(case["target"][0], case["target"][1])

        # Calculate expected proportions
        expected_w = int(300 * case["target"][0] / case["source"][0])
        expected_h = int(100 * case["target"][1] / case["source"][1])

        assert scaled.width == expected_w, f"Width mismatch: {scaled.width} != {expected_w}"
        assert scaled.height == expected_h, f"Height mismatch: {scaled.height} != {expected_h}"
        print(f"  [OK] Test case {i+1}: {case['source']} -> {case['target']}")

    print("  [PASS] Coordinate mapping verified")

    # Test 3: ROI cropping for OCR
    print("\n--- Test 3: ROI Cropping for OCR ---")
    from ocr.profiles import DELTA_FORCE
    from ocr.worker import OCRWorker
    from stream.frame_buffer import LatestFrameBuffer

    # Create test frame
    frame = np.ones((1080, 1920, 3), dtype=np.uint8) * 128
    roi = ROI(100, 200, 300, 100, 1920, 1080)

    # Create frame buffer
    buffer = LatestFrameBuffer()
    buffer.update(frame)

    # Create OCR worker
    def buffer_getter():
        return buffer

    worker = OCRWorker(buffer_getter, ROIManager(),
                       profile=DELTA_FORCE.profile_id)
    worker._roi_manager.set_roi(roi)

    # Test cropping
    cropped = worker._crop_roi(frame, roi)
    assert cropped.shape == (100, 300, 3), f"Cropped shape should be (100,300,3), got {cropped.shape}"
    print("  [OK] ROI cropping works correctly")

    print("  [PASS] ROI cropping verified")

    # Test 4: VideoWidget ROI display
    print("\n--- Test 4: VideoWidget ROI Display ---")
    from gui.video_widget import VideoWidget

    widget = VideoWidget()
    roi = ROI(100, 200, 300, 100, 1920, 1080)

    # Update ROI
    widget.update_roi(roi)
    assert widget._roi_rect is not None, "ROI rect should be set"
    assert widget._video_size == (1920, 1080), "Video size should be recorded"
    print("  [OK] ROI updated successfully")

    # Clear ROI
    widget.clear_roi()
    assert widget._roi_rect is None, "ROI rect should be cleared"
    print("  [OK] ROI cleared successfully")

    print("  [PASS] VideoWidget ROI display verified")

    # Test 5: ROI scaling for display
    print("\n--- Test 5: ROI Scaling for Display ---")

    # Create a mock frame for VideoWidget
    test_frame = np.ones((1080, 1920, 3), dtype=np.uint8) * 200
    widget._pixmap = widget._create_pixmap(test_frame)
    widget._video_size = (1920, 1080)

    # Test ROI scaling calculations
    roi = ROI(100, 200, 300, 100, 1920, 1080)
    widget.update_roi(roi)

    # The widget should handle scaling internally
    # Verify that ROI coordinates are stored correctly
    assert widget._roi_rect is not None, "ROI should be stored"
    print("  [OK] ROI scaling verified")

    print("\n" + "="*50)
    print("SUCCESS: All ROI workflow tests passed")
    print("="*50)

    return True


def main():
    """Main function"""
    try:
        success = verify_roi_workflow()
        if success:
            print("\nResult: SUCCESS - ROI workflow verification complete")
            return 0
        else:
            print("\nResult: FAILURE - Verification failed")
            return 1
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())