import telnetlib
import time
import numpy as np
import ast
import matplotlib.pyplot as plt
class DMM6500:

    def __init__(self, osa): #initializes using Telnet inputs
        host = '192.168.1.52'
        port = 23
        timeout = 10
        self.config = {
            'host': host,
            'port': port,
            'timeout': timeout
        }
        self.srate = 1000000
        selfconn = None
        self.conn = telnetlib.Telnet(**self.config)
        self.build_trigger()
        self.osa = osa
    
    def get_trace(self, osa):
        """ Return a trace in triggering window that is normalized to the ADC dynamic range """
        try:
            self.conn.write(b'init\n')
            time.sleep(0.01)
            
            self.conn.write(b'TRACe:ACTual:END?\n')
            
            endp = self.conn.read_until(b'\n',timeout = 10)
            endp = endp.decode('ascii')
            
            points = 'TRAC:DATA? 1, '+str(endp)+'\n'
            self.conn.write(points.encode())
                      
            trace = self.conn.read_until(b'\n',timeout = 10)
            trace = trace.decode('ascii')
            trace = ast.literal_eval(trace)
            
        except ValueError:
            return False
        except ConnectionAbortedError:
            print('connection error in get trace')
            return 
        finally:
            pass
        
        return (np.asarray(trace)/10)*32767

    def build_trigger(self):
        """Builds a trigger that only collects samples during the triggering window set by an AWG"""
        try:           
            self.conn.write(b'*rst\n')
            
            self.conn.write(b'sense:digitize:function "VOLT"\n')
            
            srate = 'sense:digitize:voltage:srate '+str(self.srate)+'\n'
            self.conn.write(srate.encode())
            
            buff_points = 'trace:points '+ str(self.srate)+', "defbuffer1"\n'
            self.conn.write(buff_points.encode())
            
            self.conn.write(b'TRIGger:BLOCk:BUFFer:CLEar 1\n')
            
            self.conn.write(b':TRIGger:BLOCk:WAIT 2, external\n')
            
            samples = 'TRIGger:BLOCk:MDIGitize 3, "defbuffer1", ' + str(self.srate/100) + '\n'
            self.conn.write(samples.encode())          

        except ValueError:
            return False
        except ConnectionAbortedError:
            print('connection error in build trigger')
            return False
        finally:
            return
    
    def close(self):
        """Closes the Connection"""
        if self.conn:
            self.conn.close()

