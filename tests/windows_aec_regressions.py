import ctypes, os, numpy as np
from pathlib import Path
lib=ctypes.CDLL(os.environ.get('NOVA_AEC_TEST_DLL',str(Path(__file__).resolve().parents[1]/'NovaNative/native-aec/nova_aec.dll')))
lib.nova_aec_create.argtypes=[ctypes.c_int,ctypes.POINTER(ctypes.c_void_p)];lib.nova_aec_create.restype=ctypes.c_int
lib.nova_aec_destroy.argtypes=[ctypes.c_void_p]
lib.nova_aec_feed.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint];lib.nova_aec_feed.restype=ctypes.c_int
lib.nova_aec_read.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint,ctypes.POINTER(ctypes.c_uint)];lib.nova_aec_read.restype=ctypes.c_int
rate=16000;size=rate*20;rng=np.random.default_rng(18)
noise=rng.standard_normal(size);far=np.convolve(noise,np.ones(8)/8,mode='same')*5500
# Actual echo path: delayed, attenuated direct path plus delayed room reflection.
echo=np.zeros(size);echo[1440:]+=.6*far[:-1440];echo[1920:]+=.18*far[:-1920]
near=np.zeros(size);near[rate*12:]=np.convolve(rng.standard_normal(size-rate*12),np.ones(5)/5,mode='same')*5000

def process(mic):
 h=ctypes.c_void_p();hr=lib.nova_aec_create(0,ctypes.byref(h));assert hr>=0
 result=[];out=ctypes.create_string_buffer(16384)
 try:
  for start in range(0,size,160):
   m=np.clip(mic[start:start+160],-32768,32767).astype('<i2').tobytes();r=np.clip(far[start:start+160],-32768,32767).astype('<i2').tobytes()
   hr=lib.nova_aec_feed(h,m,r,len(m));assert hr>=0,hex(hr&0xffffffff)
   while True:
    count=ctypes.c_uint();hr=lib.nova_aec_read(h,out,len(out),ctypes.byref(count));assert hr>=0,hex(hr&0xffffffff)
    if not count.value:break
    result.append(out.raw[:count.value])
 finally:lib.nova_aec_destroy(h)
 return np.frombuffer(b''.join(result),dtype='<i2').astype(float)

def rms(x):return float(np.sqrt(np.mean(x*x)))
out=process(echo);reduction=20*np.log10(rms(echo[-rate*4:])/max(1,rms(out[-rate*4:])))
print('echo-only output samples',len(out),'reduction dB',round(reduction,2),flush=True)
assert reduction>=15,'Insufficient delayed/reflected echo cancellation'
mixed=process(echo+near);ratio=rms(mixed[-rate*4:])/rms(near[-rate*4:]);print('double-talk near-end retention RMS ratio',round(ratio,3),flush=True)
# Estimate transport delay without inspecting personal or captured audio.
a=mixed[-rate*4:][::8];b=near[-rate*4:][::8]
best=max(np.corrcoef(a[max(0,lag):len(a)+min(0,lag)],b[max(0,-lag):len(b)-max(0,lag)])[0,1] for lag in range(-400,401))
print('double-talk correlation',round(float(best),3),flush=True)
assert ratio>.35 and best>.6,'Near-end interruption was not retained'
print('PASS real Windows AEC delayed/reflected echo and double-talk',flush=True)

