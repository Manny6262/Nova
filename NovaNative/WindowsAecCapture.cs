using System.Runtime.InteropServices;
namespace NovaNative;
internal interface IAecCaptureBackend
{
    int Create(out IntPtr context);
    int Read(IntPtr context, byte[] output, out uint length);
    string Stage { get; }
    void Destroy(IntPtr context);
}
internal sealed class WindowsAecCapture : IDisposable
{
    private sealed class NativeBackend : IAecCaptureBackend
    {
        [DllImport("nova_aec.dll", CallingConvention=CallingConvention.Cdecl)] private static extern int nova_aec_create(int source,out IntPtr context);
        [DllImport("nova_aec.dll", CallingConvention=CallingConvention.Cdecl)] private static extern int nova_aec_read(IntPtr context,byte[] output,uint capacity,out uint length);
        [DllImport("nova_aec.dll", CallingConvention=CallingConvention.Cdecl)] private static extern void nova_aec_destroy(IntPtr context);
        [DllImport("nova_aec.dll", CallingConvention=CallingConvention.Cdecl)] private static extern IntPtr nova_aec_stage();
        public int Create(out IntPtr context)=>nova_aec_create(1,out context);
        public int Read(IntPtr context,byte[] output,out uint length)=>nova_aec_read(context,output,(uint)output.Length,out length);
        public string Stage { get { try{return Marshal.PtrToStringAnsi(nova_aec_stage())??"unknown";}catch{return "native component load";} } }
        public void Destroy(IntPtr context)=>nova_aec_destroy(context);
    }
    private readonly IAecCaptureBackend backend;
    private readonly CancellationTokenSource stop=new();
    private readonly TaskCompletionSource ready=new(TaskCreationOptions.RunContinuationsAsynchronously);
    private Thread? worker;
    private int disposed;
    public WindowsAecCapture(IAecCaptureBackend? backend=null){this.backend=backend??new NativeBackend();}
    public Task StartAsync(Action<byte[]> audio,Action<string> failed)
    {
        worker=new Thread(()=>
        {
            IntPtr context=IntPtr.Zero;
            try
            {
                var result=backend.Create(out context);
                if(result<0)throw new COMException($"Windows AEC failed at {backend.Stage} (0x{result:X8}). Retry voice and report this stage/code if it repeats.",result);
                if(stop.IsCancellationRequested){ready.TrySetCanceled(stop.Token);return;}
                ready.TrySetResult();var buffer=new byte[6400];
                while(!stop.IsCancellationRequested)
                {
                    result=backend.Read(context,buffer,out var length);
                    if(result<0)throw new COMException($"Windows AEC stopped at {backend.Stage} (0x{result:X8}). Retry voice.",result);
                    if(length>0&&!stop.IsCancellationRequested)audio(buffer.AsSpan(0,(int)length).ToArray());
                    else if(stop.Token.WaitHandle.WaitOne(10))break;
                }
            }
            catch(Exception error)
            {
                var wasReady=ready.Task.IsCompletedSuccessfully;
                ready.TrySetException(error);
                // Startup errors belong to the awaiting UI handler; reporting
                // them twice could invalidate its generation and hide details.
                if(wasReady&&!stop.IsCancellationRequested)failed(error is COMException?error.Message:$"AEC capture failed (0x{error.HResult:X8}). Retry voice.");
            }
            finally{if(context!=IntPtr.Zero)backend.Destroy(context);}
        }){IsBackground=true,Name="Nova Windows AEC"};
        worker.Start();return ready.Task;
    }
    public void Dispose()
    {
        if(Interlocked.Exchange(ref disposed,1)!=0)return;
        stop.Cancel();if(worker?.Join(1500)==true)stop.Dispose();
    }
}
