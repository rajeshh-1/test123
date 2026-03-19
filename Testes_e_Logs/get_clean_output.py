import sys
import io
import traceback
sys.path.append('.')
import arbitrage_scanner

with open('clean_output_log.txt', 'w', encoding='utf-8') as f:
    # capture print output
    original_stdout = sys.stdout
    sys.stdout = f
    try:
        arbitrage_scanner.run_scanner()
    except Exception as e:
        print("Erro: ", e)
        traceback.print_exc()
    finally:
        sys.stdout = original_stdout
