from mlpy import mdaio
import numpy as np
import multiprocessing
import time
import os

# import h5py
import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)
import h5py
warnings.resetwarnings()


class SharedChunkInfo():
    def __init__(self,num_chunks):
        self.timer_timestamp = multiprocessing.Value('d',time.time(),lock=False)
        self.num_chunks=num_chunks
        self.num_completed_chunks = multiprocessing.Value('l',0,lock=False)
        self.lock = multiprocessing.Lock()
    def acquireLock(self):
        self.lock.acquire()
    def releaseLock(self):
        self.lock.release()
    def reportChunkCompleted(self,num):
        self.num_completed_chunks.value+=1
    def resetTimer(self):
        self.timer_timestamp.value=time.time()
    def elapsedTime(self):
        return time.time()-self.timer_timestamp.value
    def printStatus(self):
        print('Processed {} of {} chunks...'.format(self.num_completed_chunks.value,self.num_chunks))

def compute_AAt_matrix_for_chunk(num):
    opts=g_opts
    in_fname=opts['timeseries'] # The entire (large) input file
    out_fname=opts['timeseries_out'] # The entire (large) output file
    chunk_size=opts['chunk_size']
    
    X=mdaio.DiskReadMda(in_fname)
    
    t1=int(num*opts['chunk_size']) # first timepoint of the chunk
    t2=int(np.minimum(X.N2(),(t1+chunk_size))) # last timepoint of chunk (+1)
    
    chunk=X.readChunk(i1=0,N1=X.N1(),i2=t1,N2=t2-t1) # Read the chunk
    
    ret=chunk @ np.transpose(chunk)
    
    return ret

def whiten_chunk(num,W):
    #print('Whitening {}'.format(num))
    opts=g_opts
    #print('Whitening chunk {} of {}'.format(num,opts['num_chunks']))
    in_fname=opts['timeseries'] # The entire (large) input file
    out_fname=opts['timeseries_out'] # The entire (large) output file
    temp_fname=opts['temp_fname'] # The temporary hdf5 file to accumulate the whitened chunks
    chunk_size=opts['chunk_size']
    
    X=mdaio.DiskReadMda(in_fname)
    
    t1=int(num*opts['chunk_size']) # first timepoint of the chunk
    t2=int(np.minimum(X.N2(),(t1+chunk_size))) # last timepoint of chunk (+1)
    
    chunk=X.readChunk(i1=0,N1=X.N1(),i2=t1,N2=t2-t1) # Read the chunk
    
    chunk=W @ chunk

    ## Lock   ###########################################################
    g_shared_data.acquireLock() 
    
    # Save result to the temporary file
    with h5py.File(temp_fname,"a") as f:
        f.create_dataset('whitened-{}'.format(num),data=chunk)
    
    # Report that we have completed this chunk
    g_shared_data.reportChunkCompleted(num) 

    # Print status if it has been long enough since last report
    if g_shared_data.elapsedTime()>4:
        g_shared_data.printStatus()
        g_shared_data.resetTimer()
        
    g_shared_data.releaseLock()
    ## Unlock ###########################################################
    
def whiten(*,
        timeseries,timeseries_out,
        chunk_size=30000*10,num_processes=os.cpu_count()
        ):
    """
    Whiten a multi-channel timeseries

    Parameters
    ----------
    timeseries : INPUT
        MxN raw timeseries array (M = #channels, N = #timepoints)
        
    timeseries_out : OUTPUT
        Whitened output (MxN array)

    """

    tempdir=os.environ.get('ML_PROCESSOR_TEMPDIR')
    if not tempdir:
        print ('Warning: environment variable ML_PROCESSOR_TEMPDIR not set. Using current directory.')
        tempdir='.'
    print ('Using tempdir={}'.format(tempdir))

    X=mdaio.DiskReadMda(timeseries)
    M=X.N1() # Number of channels
    N=X.N2() # Number of timepoints

    num_chunks_for_computing_cov_matrix=10
    
    num_chunks=int(np.ceil(N/chunk_size))
    print ('Chunk size: {}, Num chunks: {}, Num processes: {}'.format(chunk_size,num_chunks,num_processes))
    
    opts={
        "timeseries":timeseries,
        "timeseries_out":timeseries_out,
        "temp_fname":tempdir+'/whitened_chunks.hdf5',
        "chunk_size":chunk_size,
        "num_processes":num_processes,
        "num_chunks":num_chunks
    }
    global g_opts
    g_opts=opts
    
    pool = multiprocessing.Pool(processes=num_processes)
    step=int(np.maximum(1,np.floor(num_chunks/num_chunks_for_computing_cov_matrix)))
    AAt_matrices=pool.map(compute_AAt_matrix_for_chunk,range(0,num_chunks,step),chunksize=1)
    
    AAt=np.zeros((M,M),dtype='float64')
    
    for M0 in AAt_matrices:
        AAt+=M0/(len(AAt_matrices)*chunk_size) ##important: need to fix the denominator here to account for possible smaller chunk
    
    U, S, Ut = np.linalg.svd(AAt, full_matrices=True)
    
    W = (U @ np.diag(1/np.sqrt(S))) @ Ut
    #print ('Whitening matrix:')
    #print (W)
    
    global g_shared_data
    g_shared_data=SharedChunkInfo(num_chunks)
    mdaio.writemda32(np.zeros([M,0]),timeseries_out)
    
    pool = multiprocessing.Pool(processes=num_processes)
    pool.starmap(whiten_chunk,[(num,W) for num in range(0,num_chunks)],chunksize=1)

    print('Assembling whitened chunks...')
    mdaio.writemda32(np.zeros([M,0]),timeseries_out,force_64bitdims=True)
    for num in range(num_chunks):
        with h5py.File(opts['temp_fname'], "a") as f:
            Y=np.array(f.get('whitened-{}'.format(num)))
            assert Y.shape[0] == M 
            mdaio.appendmda(Y,timeseries_out)
    return True
    
    return True
whiten.name='pyms.whiten'
whiten.version='0.1.1'