#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <dmo.h>
#include <uuids.h>
#include <mmreg.h>
#include <wmcodecdsp.h>
#include <propsys.h>
#include <mmdeviceapi.h>
#include <vector>
#include <algorithm>

class Buffer final : public IMediaBuffer {
 LONG refs=1; DWORD length=0; std::vector<BYTE> data;
public:
 Buffer(DWORD capacity):data(capacity){}
 STDMETHODIMP QueryInterface(REFIID iid,void** value) override {if(iid==IID_IUnknown||iid==__uuidof(IMediaBuffer)){*value=this;AddRef();return S_OK;}*value=nullptr;return E_NOINTERFACE;}
 STDMETHODIMP_(ULONG) AddRef() override{return InterlockedIncrement(&refs);}
 STDMETHODIMP_(ULONG) Release() override{auto left=InterlockedDecrement(&refs);if(!left)delete this;return left;}
 STDMETHODIMP SetLength(DWORD n) override{if(n>data.size())return E_INVALIDARG;length=n;return S_OK;}
 STDMETHODIMP GetMaxLength(DWORD* n) override{*n=(DWORD)data.size();return S_OK;}
 STDMETHODIMP GetBufferAndLength(BYTE** p,DWORD* n) override{if(p)*p=data.data();if(n)*n=length;return S_OK;}
 void assign(const BYTE* p,DWORD n){std::copy(p,p+n,data.begin());length=n;}
};
struct Context {IMediaObject* dmo=nullptr;bool com=false;LONGLONG timestamp=0;};
static HRESULT prop(IPropertyStore* store,REFPROPERTYKEY key,LONG value,VARTYPE type=VT_I4){PROPVARIANT v{};v.vt=type;if(type==VT_BOOL)v.boolVal=value?VARIANT_TRUE:VARIANT_FALSE;else v.lVal=value;return store->SetValue(key,v);}
static HRESULT type(IMediaObject* dmo,bool input,DWORD stream){
 WAVEFORMATEX format{WAVE_FORMAT_PCM,1,16000,32000,2,16,0};
 DMO_MEDIA_TYPE mt{};mt.majortype=MEDIATYPE_Audio;mt.subtype=MEDIASUBTYPE_PCM;mt.bFixedSizeSamples=TRUE;mt.lSampleSize=2;mt.formattype=FORMAT_WaveFormatEx;mt.cbFormat=sizeof(format);mt.pbFormat=(BYTE*)&format;
 return input?dmo->SetInputType(stream,&mt,0):dmo->SetOutputType(0,&mt,0);
}
static HRESULT device_index(IMMDeviceEnumerator* enumerator,EDataFlow flow,UINT* index){
 IMMDevice* current=nullptr;IMMDeviceCollection* devices=nullptr;LPWSTR currentId=nullptr;
 HRESULT hr=enumerator->GetDefaultAudioEndpoint(flow,eConsole,&current);
 if(SUCCEEDED(hr))hr=current->GetId(&currentId);
 if(SUCCEEDED(hr))hr=enumerator->EnumAudioEndpoints(flow,DEVICE_STATE_ACTIVE,&devices);
 UINT count=0;if(SUCCEEDED(hr))hr=devices->GetCount(&count);
 bool found=false;
 for(UINT i=0;SUCCEEDED(hr)&&i<count;i++){IMMDevice* device=nullptr;LPWSTR id=nullptr;hr=devices->Item(i,&device);if(SUCCEEDED(hr))hr=device->GetId(&id);if(SUCCEEDED(hr)&&wcscmp(id,currentId)==0){*index=i;found=true;}CoTaskMemFree(id);if(device)device->Release();if(found)break;}
 CoTaskMemFree(currentId);if(current)current->Release();if(devices)devices->Release();return FAILED(hr)?hr:found?S_OK:E_FAIL;
}
extern "C" __declspec(dllexport) void __cdecl nova_aec_destroy(Context* ctx){if(!ctx)return;if(ctx->dmo){ctx->dmo->FreeStreamingResources();ctx->dmo->Release();}if(ctx->com)CoUninitialize();delete ctx;}
static thread_local const char* last_stage = "idle";
extern "C" __declspec(dllexport) const char* __cdecl nova_aec_stage(){return last_stage;}
extern "C" __declspec(dllexport) HRESULT __cdecl nova_aec_probe_devices(UINT* capture,UINT* render){
 last_stage="COM initialization";HRESULT hr=CoInitializeEx(nullptr,COINIT_MULTITHREADED);bool com=SUCCEEDED(hr);
 if(FAILED(hr)&&hr!=RPC_E_CHANGED_MODE)return hr;
 IMMDeviceEnumerator* enumerator=nullptr;
 last_stage="device enumerator";hr=CoCreateInstance(__uuidof(MMDeviceEnumerator),nullptr,CLSCTX_INPROC_SERVER,IID_PPV_ARGS(&enumerator));
 if(SUCCEEDED(hr)){last_stage="default microphone";hr=device_index(enumerator,eCapture,capture);}
 if(SUCCEEDED(hr)){last_stage="default speakers";hr=device_index(enumerator,eRender,render);}
 if(enumerator)enumerator->Release();if(com)CoUninitialize();return hr;
}
#define STEP(name, expression) do{if(SUCCEEDED(hr)){last_stage=name;hr=(expression);}}while(0)
extern "C" __declspec(dllexport) DWORD __cdecl nova_aec_pack_devices(UINT capture,UINT render){return ((render & 0xffffu)<<16)|(capture & 0xffffu);}
static HRESULT create_context(int source,Context** result,bool allocate){
 *result=nullptr;auto ctx=new Context();last_stage="COM initialization";HRESULT hr=CoInitializeEx(nullptr,COINIT_MULTITHREADED);ctx->com=SUCCEEDED(hr);if(FAILED(hr)&&hr!=RPC_E_CHANGED_MODE){delete ctx;return hr;}
 const CLSID clsid={0x745057c7,0xf353,0x4f2d,{0xa7,0xee,0x58,0x43,0x44,0x77,0x73,0x0e}};
 last_stage="Windows AEC component";hr=CoCreateInstance(clsid,nullptr,CLSCTX_INPROC_SERVER,__uuidof(IMediaObject),(void**)&ctx->dmo);
 IPropertyStore* store=nullptr;
 STEP("AEC properties",ctx->dmo->QueryInterface(IID_PPV_ARGS(&store)));
 STEP("capture mode",prop(store,MFPKEY_WMAAECMA_DMO_SOURCE_MODE,source,VT_BOOL));
 STEP("AEC system mode",prop(store,MFPKEY_WMAAECMA_SYSTEM_MODE,SINGLE_CHANNEL_AEC));
 STEP("AEC features",prop(store,MFPKEY_WMAAECMA_FEATURE_MODE,1,VT_BOOL));
 STEP("gain control",prop(store,MFPKEY_WMAAECMA_FEATR_AGC,0,VT_BOOL));
 STEP("noise suppression",prop(store,MFPKEY_WMAAECMA_FEATR_NS,0,VT_I4));
 STEP("residual echo suppression",prop(store,MFPKEY_WMAAECMA_FEATR_AES,2));
 STEP("echo duration",prop(store,MFPKEY_WMAAECMA_FEATR_ECHO_LENGTH,512));
 if(source&&SUCCEEDED(hr)){
  IMMDeviceEnumerator* enumerator=nullptr;UINT capture=0,render=0;
  STEP("device enumerator",CoCreateInstance(__uuidof(MMDeviceEnumerator),nullptr,CLSCTX_INPROC_SERVER,IID_PPV_ARGS(&enumerator)));
  STEP("default microphone",device_index(enumerator,eCapture,&capture));
  STEP("default speakers",device_index(enumerator,eRender,&render));
  STEP("device selection",prop(store,MFPKEY_WMAAECMA_DEVICE_INDEXES,nova_aec_pack_devices(capture,render)));
  if(enumerator)enumerator->Release();
 }
 if(store)store->Release();
 STEP("output format",type(ctx->dmo,false,0));
 if(!source){STEP("microphone format",type(ctx->dmo,true,0));STEP("reference format",type(ctx->dmo,true,1));}
 if(allocate){STEP("capture allocation",ctx->dmo->AllocateStreamingResources());}
 if(FAILED(hr)){nova_aec_destroy(ctx);return hr;}last_stage="ready";*result=ctx;return S_OK;
}
extern "C" __declspec(dllexport) HRESULT __cdecl nova_aec_create(int source,Context** result){return create_context(source,result,true);}
// Configure and read back source-mode properties without allocating capture.
extern "C" __declspec(dllexport) HRESULT __cdecl nova_aec_source_preflight(DWORD* packed,DWORD* sample_rate){
 Context* ctx=nullptr;HRESULT hr=create_context(1,&ctx,false);if(FAILED(hr))return hr;
 IPropertyStore* store=nullptr;PROPVARIANT value{};
 hr=ctx->dmo->QueryInterface(IID_PPV_ARGS(&store));
 if(SUCCEEDED(hr))hr=store->GetValue(MFPKEY_WMAAECMA_DEVICE_INDEXES,&value);
 if(SUCCEEDED(hr)&&value.vt!=VT_I4)hr=E_INVALIDARG;
 if(SUCCEEDED(hr))*packed=(DWORD)value.lVal;
 PropVariantClear(&value);if(store)store->Release();
 DMO_MEDIA_TYPE mt{};if(SUCCEEDED(hr))hr=ctx->dmo->GetOutputCurrentType(0,&mt);
 if(SUCCEEDED(hr)){*sample_rate=((WAVEFORMATEX*)mt.pbFormat)->nSamplesPerSec;MoFreeMediaType(&mt);}
 nova_aec_destroy(ctx);return hr;
}
extern "C" __declspec(dllexport) HRESULT __cdecl nova_aec_read(Context* ctx,BYTE* output,DWORD capacity,DWORD* length){
 last_stage="capture read";*length=0;auto buffer=new Buffer(capacity);DMO_OUTPUT_DATA_BUFFER out{};out.pBuffer=buffer;DWORD flags=0;
 HRESULT hr=ctx->dmo->ProcessOutput(0,1,&out,&flags);BYTE* bytes=nullptr;DWORD count=0;
 if(SUCCEEDED(hr)){buffer->GetBufferAndLength(&bytes,&count);std::copy(bytes,bytes+count,output);*length=count;}
 buffer->Release();return hr;
}
extern "C" __declspec(dllexport) HRESULT __cdecl nova_aec_feed(Context* ctx,const BYTE* mic,const BYTE* reference,DWORD length){
 auto capture=new Buffer(length);auto render=new Buffer(length);capture->assign(mic,length);render->assign(reference,length);
 auto duration=(LONGLONG)length*10000000/32000;
 HRESULT hr=ctx->dmo->ProcessInput(1,render,DMO_INPUT_DATA_BUFFERF_TIME|DMO_INPUT_DATA_BUFFERF_TIMELENGTH,ctx->timestamp,duration);
 if(SUCCEEDED(hr))hr=ctx->dmo->ProcessInput(0,capture,DMO_INPUT_DATA_BUFFERF_TIME|DMO_INPUT_DATA_BUFFERF_TIMELENGTH,ctx->timestamp,duration);
 ctx->timestamp+=duration;capture->Release();render->Release();return hr;
}




