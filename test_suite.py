"""
Comprehensive test suite for the synthesizer components
"""

# Global imports
import sys
import time

# Local imports
from music import MusicEngine, midi_to_note
from keyboard_handler import KeyboardHandler, test_keyboard
from audio_engine import AudioEngine, test_basic_audio, test_streaming_audio

def test_music_engine():
    """Test 1: Music Engine Test"""
    print("=" * 50)
    print("TEST 1: Music Engine")
    print("=" * 50)
    
    try:
        music = MusicEngine(config_file='config.ini')
        
        print("\n✅ Music engine loaded successfully")
        
        # Test some frequencies
        test_notes = ['C4', 'D4', 'E4', 'C#4', 'D#4']
        print("\n🎵 Testing some note frequencies:")
        for note in test_notes:
            freq = music.note_to_frequency(note)
            print(f"  {note} -> {freq:.1f} Hz")
        
        # Test tuning change
        print("\n🎼 Testing tuning change:")
        original_freq = music.note_to_frequency('A4')
        print(f"  A4 at 440 Hz: {original_freq:.1f} Hz")
        
        music.set_tuning(442.0)  # Slightly higher tuning
        new_freq = music.note_to_frequency('A4')
        print(f"  A4 at 442 Hz: {new_freq:.1f} Hz")
        
        # Reset to standard tuning
        music.set_tuning(440.0)
        
        return True
        
    except Exception as e:
        print(f"❌ Music engine test failed: {e}")
        return False

def test_keyboard_handler():
    """Test 2: Keyboard Handler Test"""
    print("=" * 50)
    print("TEST 2: Keyboard Handler")
    print("=" * 50)
    
    try:
        print("🎹 Testing keyboard handler initialization...")
        keyboard = KeyboardHandler('config.ini')
        valid_keys = keyboard.get_all_keys()
        
        print("✅ Keyboard handler created successfully")
        print(f"📝 Monitoring {len(valid_keys)} keys")
        
        # Test volume control
        print("\n🔊 Testing volume control:")
        keyboard.set_volume(0.3)
        print(f"  Set volume to 30%: {keyboard.get_volume()*100:.0f}%")
        
        keyboard.set_volume(0.8)
        print(f"  Set volume to 80%: {keyboard.get_volume()*100:.0f}%")
        
        # Interactive test
        print("\n🔍 Interactive keyboard test available")
        print("  Would you like to test keyboard input? (y/n): ", end="")
        
        try:
            choice = input().lower().strip()
            if choice == 'y':
                print("\n⌨️  Starting interactive keyboard test...")
                print("Press keys (ESC to finish):")
                return test_keyboard()
            else:
                print("Skipping interactive test")
                return True
        except KeyboardInterrupt:
            print("\nTest interrupted")
            return True
            
    except Exception as e:
        print(f"❌ Keyboard handler test failed: {e}")
        return False

def test_audio_basic():
    """Test 3: Basic Audio Test"""
    print("=" * 50)
    print("TEST 3: Basic Audio")
    print("=" * 50)
    
    return test_basic_audio(duration=2.0, frequency=440.0)

def test_audio_streaming():
    """Test 4: Streaming Audio Test"""
    print("=" * 50)
    print("TEST 4: Streaming Audio")
    print("=" * 50)
    
    return test_streaming_audio(duration=3.0)

def test_audio_engine():
    """Test 5: Audio Engine Test"""
    print("=" * 50)
    print("TEST 5: Audio Engine")
    print("=" * 50)
    
    try:
        print("🔊 Creating audio engine...")
        audio = AudioEngine('config.ini')
        
        print("✅ Audio engine created successfully")
        
        # Test device info
        audio.print_device_info()
        
        print("\n🎵 Testing audio engine with multiple frequencies...")
        
        # Start the audio engine
        audio.start()  # Raises SynthError on failure
        
        print("✅ Audio engine started")
        
        # Test playing notes
        test_freqs = {'C': 261.63, 'E': 329.63, 'G': 392.00}  # C major chord
        
        print("\n🎶 Playing C major chord (flute) for 2 seconds...")
        for note, freq in test_freqs.items():
            audio.play_note(note, freq, instrument_type='flute')
            print(f"  Added {note}: {freq} Hz")
        
        time.sleep(2.0)
        
        print("\n🎵 Releasing notes one by one...")
        for note in test_freqs:
            audio.stop_note(note)
            print(f"  Released {note}")
            time.sleep(0.5)
        
        print("\n🎸 Playing the same chord with the plucked string...")
        for note, freq in test_freqs.items():
            audio.play_note(note, freq, instrument_type='pluck')
            time.sleep(0.2)
        
        time.sleep(2.0)
        for note in test_freqs:
            audio.stop_note(note)
        
        # Test volume
        print("\n🔊 Testing volume changes...")
        audio.play_note('A', 440.0, instrument_type='flute')
        
        for volume in [0.2, 0.5, 0.8, 0.5]:
            audio.set_volume(volume)
            print(f"  Volume: {volume*100:.0f}%")
            time.sleep(0.5)
        
        audio.stop_note('A')
        time.sleep(0.5)
        audio.stop()
        
        print("✅ Audio engine test completed successfully")
        return True
        
    except Exception as e:
        print(f"❌ Audio engine test failed: {e}")
        return False

def test_full_integration():
    """Test 6: Full Integration Test"""
    print("=" * 50)
    print("TEST 6: Full Integration")
    print("=" * 50)
    
    try:
        print("🔧 Testing full synthesizer integration...")
        
        # This would import and test the full synthesizer
        # But we'll do a simpler integration test here
        
        music = MusicEngine()
        keyboard = KeyboardHandler('config.ini')
        audio = AudioEngine('config.ini')
        
        print("✅ All components initialized")
        
        # Test callbacks
        def on_note_on(key, midi):
            freq = music.midi_to_frequency(midi)
            audio.play_note(key, freq)
            print(f"♪ {key} -> {midi_to_note(midi)} ({freq:.1f} Hz)")
        
        def on_note_off(key, midi):
            audio.stop_note(key)
            print(f"♫ {key} -> {midi_to_note(midi)} (off)")
        
        keyboard.set_callbacks(on_note_on=on_note_on, on_note_off=on_note_off)
        
        print("✅ Callbacks configured")
        
        # Start audio
        audio.start()  # Raises SynthError on failure
        
        print("✅ Integration test setup completed")
        print("📋 Would test keyboard integration, but that requires interactive input")
        
        audio.stop()
        
        return True
        
    except Exception as e:
        print(f"❌ Integration test failed: {e}")
        return False

def run_all_tests():
    """Run all tests"""
    print("🧪 SYNTHESIZER TEST SUITE")
    print("=" * 60)
    
    tests = [
        ("Music Engine", test_music_engine),
        ("Keyboard Handler", test_keyboard_handler),
        ("Basic Audio", test_audio_basic),
        ("Streaming Audio", test_audio_streaming),
        ("Audio Engine", test_audio_engine),
        ("Full Integration", test_full_integration),
    ]
    
    results = []
    
    for test_name, test_func in tests:
        print(f"\n🔍 Running {test_name} test...")
        try:
            result = test_func()
            results.append((test_name, result))
            if result:
                print(f"✅ {test_name} test PASSED")
            else:
                print(f"❌ {test_name} test FAILED")
        except Exception as e:
            print(f"❌ {test_name} test CRASHED: {e}")
            results.append((test_name, False))
    
    # Summary
    print("\n" + "=" * 60)
    print("📊 TEST RESULTS SUMMARY")
    print("=" * 60)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for test_name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"  {test_name:20} {status}")
    
    print(f"\n🎯 Overall: {passed}/{total} tests passed ({passed/total*100:.1f}%)")
    
    if passed == total:
        print("🎉 All tests passed! Your synthesizer should work correctly.")
    else:
        print("⚠️  Some tests failed. Check the details above.")
    
    return passed == total

def main():
    """Main test runner"""
    if len(sys.argv) > 1:
        # Run specific test
        test_num = sys.argv[1]
        
        test_map = {
            '1': ("Music Engine", test_music_engine),
            '2': ("Keyboard Handler", test_keyboard_handler),
            '3': ("Basic Audio", test_audio_basic),
            '4': ("Streaming Audio", test_audio_streaming),
            '5': ("Audio Engine", test_audio_engine),
            '6': ("Full Integration", test_full_integration),
        }
        
        if test_num in test_map:
            test_name, test_func = test_map[test_num]
            print(f"Running {test_name} test...")
            result = test_func()
            return 0 if result else 1
        else:
            print("Invalid test number. Available tests:")
            for num, (name, _) in test_map.items():
                print(f"  {num}: {name}")
            return 1
    else:
        # Interactive menu
        print("🧪 SYNTHESIZER TEST SUITE")
        print("=" * 40)
        print("Select a test to run:")
        print("  1: Music Engine Test")
        print("  2: Keyboard Handler Test")
        print("  3: Basic Audio Test")
        print("  4: Streaming Audio Test")
        print("  5: Audio Engine Test")
        print("  6: Full Integration Test")
        print("  a: Run All Tests")
        print("  q: Quit")
        
        while True:
            choice = input("\nEnter your choice: ").lower().strip()
            
            if choice == 'q':
                print("Goodbye!")
                return 0
            elif choice == 'a':
                success = run_all_tests()
                return 0 if success else 1
            elif choice in '123456':
                test_map = {
                    '1': test_music_engine,
                    '2': test_keyboard_handler,
                    '3': test_audio_basic,
                    '4': test_audio_streaming,
                    '5': test_audio_engine,
                    '6': test_full_integration,
                }
                result = test_map[choice]()
                print(f"\nTest {'PASSED' if result else 'FAILED'}")
            else:
                print("Invalid choice. Please try again.")

if __name__ == "__main__":
    print("Running test suite...")
    sys.exit(main()) 