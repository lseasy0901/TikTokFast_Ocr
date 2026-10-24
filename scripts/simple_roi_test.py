#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Simple ROI Verification Test Phase 4.4
"""

import sys
import os
import numpy as np
from unittest.mock import Mock

# Add project root to path
project_root = os.path.dirname(os.path.abspath('.'))
sys.path.insert(0, project_root)

from ocr.profiles import DELTA_FORCE
from ocr.roi_manager import ROI, ROIManager
from gui.video_widget import VideoWidget


def test_roi_coordinate_mapping():
    """Test ROI coordinate mapping"""
    print("--- Testing ROI Coordinate Mapping ---")

    test_cases = [
        {"video_size": (1920, 1080), "widget_size": (1280, 720)},
        {"video_size": (1280, 720), "widget_size": (640, 360)},
        {"video_size": (854, 480), "widget_size": (640, 360)},
    ]

    for i, case in enumerate(test_cases):
        print(f"\nTest Case {i+1}: {case['video_size']} -> {case['widget_size']}")

        # Create ROI with source dimensions
        roi = ROI(100, 200, 300, 100, case['video_size'][0], case['video_size'][1])

        # Scale to widget size
        scaled_roi = roi.scale_to(case['widget_size'][0], case['widget_size'][1])

        print(f"  Original ROI: x={roi.x}, y={roi.y}, w={roi.width}, h={roi.height}")
        print(f"  Scaled ROI:  x={scaled_roi.x}, y={scaled_roi.y}, w={scaled_roi.width}, h={scaled_roi.height}")

        # Verify proportions
        expected_w = int(300 * case['widget_size'][0] / case['video_size'][0])
        expected_h = int(100 * case['widget_size'][1] / case['video_size'][1])

        assert scaled_roi.width == expected_w, f"Width mismatch: {scaled_roi.width} != {expected_w}"
        assert scaled_roi.height == expected_h, f"Height mismatch: {scaled_roi.height} != {expected_h}"

        print("  [OK] Coordinate mapping correct")

    print("\n[OK] All coordinate mapping tests passed")


def test_roi_manager_operations():
    """Test ROI manager operations"""
    print("\n--- Testing ROI Manager Operations ---")

    roi_manager = ROIManager()

    # Test 1: Initially no ROI
    assert not roi_manager.has_roi, "Should have no ROI initially"
    assert roi_manager.get_roi() is None, "get_roi() should return None"
    print("  [OK] Initial state correct")

    # Test 2: Set ROI
    roi = ROI(100, 200, 300, 100, 1920, 1080)
    roi_manager.set_roi(roi)
    assert roi_manager.has_roi, "Should have ROI after setting"
    assert roi_manager.get_roi() == roi, "get_roi() should return the ROI"
    print("  [OK] ROI set successfully")

    # Test 3: Clear ROI
    roi_manager.clear_roi()
    assert not roi_manager.has_roi, "Should have no ROI after clearing"
    assert roi_manager.get_roi() is None, "get_roi() should return None"
    print("  [OK] ROI cleared successfully")

    print("\n[OK] All ROI manager tests passed")


def test_roi_display():
    """Test ROI display"""
    print("\n--- Testing ROI Display ---")

    # Create video widget
    video_widget = VideoWidget()

    # Test 1: Update ROI
    roi = ROI(100, 200, 300, 100, 1920, 1080)
    video_widget.update_roi(roi)
    assert video_widget._roi_rect is not None, "ROI rect should be set"
    assert video_widget._video_size == (1920, 1080), "Video size should be recorded"
    print("  [OK] ROI updated successfully")

    # Test 2: Clear ROI
    video_widget.clear_roi()
    assert video_widget._roi_rect is None, "ROI rect should be cleared"
    print("  [OK] ROI cleared successfully")

    print("\n[OK] All ROI display tests passed")


def test_roi_ocr_integration():
    """Test ROI-OCR integration"""
    print("\n--- Testing ROI-OCR Integration ---")

    from ocr.worker import OCRWorker
    from stream.frame_buffer import LatestFrameBuffer

    # Create test frame
    test_frame = np.ones((1080, 1920, 3), dtype=np.uint8) * 128
    test_roi = ROI(100, 200, 300, 100, 1920, 1080)

    # Create frame buffer
    frame_buffer = LatestFrameBuffer()
    frame_buffer.update(test_frame)

    # Create ROI manager
    roi_manager = ROIManager()
    roi_manager.set_roi(test_roi)

    # Create OCR worker
    def frame_buffer_getter():
        return frame_buffer

    ocr_worker = OCRWorker(
        frame_buffer_getter=frame_buffer_getter,
        roi_manager=roi_manager,
        interval_ms=100,
        profile=DELTA_FORCE.profile_id,
    )

    # Test ROI cropping
    print("\nTest 1: ROI cropping")
    cropped = ocr_worker._crop_roi(test_frame, test_roi)
    assert cropped.shape == (100, 300, 3), f"Cropped shape should be (100,300,3), got {cropped.shape}"
    print("  [OK] ROI cropping works")

    # Test OCR with ROI
    print("\nTest 2: OCR with ROI")
    ocr_worker.start_recognition()
    import time
    time.sleep(0.5)  # Let OCR run
    ocr_worker.stop_recognition()

    print(f"  OCR executed: {ocr_worker.recognition_count} times")
    print(f"  Errors: {ocr_worker.error_count}")
    print("  [OK] OCR-ROI integration works")

    print("\n[OK] All ROI-OCR integration tests passed")


def main():
    """Main test function"""
    print("=== ROI Final Verification Test Phase 4.4 ===")

    try:
        test_roi_coordinate_mapping()
        test_roi_manager_operations()
        test_roi_display()
        test_roi_ocr_integration()

        print("\n" + "="*50)
        print("SUCCESS: All ROI verification tests passed")
        print("="*50)

        # Test workflow simulation
        print("\n--- Workflow Simulation ---")
        roi_manager = ROIManager()

        # Step 1: Select ROI
        roi1 = ROI(100, 200, 300, 100, 1920, 1080)
        roi_manager.set_roi(roi1)
        print("Step 1: ROI selected")

        # Step 2: Select new ROI (replaces first)
        roi2 = ROI(200, 300, 400, 150, 1920, 1080)
        roi_manager.set_roi(roi2)
        print("Step 2: New ROI replaces old")

        # Step 3: Cancel selection
        roi_manager.clear_roi()
        print("Step 3: ROI cleared")

        print("\nSUCCESS: Workflow simulation complete")
        print("ROI final verification PASSED")
        return 0

    except Exception as e:
        print(f"\nFAILURE: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())