import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Progress } from '@/components/ui/progress';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { IModalProps } from '@/interfaces/common';
import { UploadProgress } from '@/utils/multipart-upload';
import { Dispatch, SetStateAction, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { FileUploader } from '../file-uploader';

const ACCEPTED_FILE_TYPES = {
  'application/*': [],
  'text/*': [],
  'image/*': [],
  'audio/*': [],
  'video/*': [],
};

function UploaderTabs({
  files,
  setFiles,
  disabled,
  progresses,
}: {
  files: File[];
  setFiles: Dispatch<SetStateAction<File[]>>;
  disabled?: boolean;
  progresses?: Record<string, number>;
}) {
  const { t } = useTranslation();

  return (
    <Tabs defaultValue="account">
      <TabsList className="grid w-full grid-cols-2 mb-4">
        <TabsTrigger value="account">{t('fileManager.local')}</TabsTrigger>
        <TabsTrigger value="password">{t('fileManager.s3')}</TabsTrigger>
      </TabsList>
      <TabsContent value="account">
        <FileUploader
          value={files}
          accept={ACCEPTED_FILE_TYPES}
          maxFileCount={128}
          maxSize={1024 * 1024 * 1024}
          onValueChange={setFiles}
          disabled={disabled}
          progresses={progresses}
        />
      </TabsContent>
      <TabsContent value="password">{t('common.comingSoon')}</TabsContent>
    </Tabs>
  );
}

export function FileUploadDialog({
  hideModal,
  loading,
  onOk,
}: Omit<IModalProps<File[]>, 'onOk'> & {
  onOk?: (
    files: File[],
    options: { onProgress: (progress: UploadProgress) => void },
  ) => Promise<any> | void;
}) {
  const { t } = useTranslation();
  const [files, setFiles] = useState<File[]>([]);
  const [progresses, setProgresses] = useState<Record<string, number>>({});
  const [latestProgress, setLatestProgress] = useState<UploadProgress | null>(
    null,
  );

  const handleProgress = (progress: UploadProgress) => {
    setLatestProgress(progress);
    if (progress.fileName) {
      setProgresses((prev) => ({
        ...prev,
        [progress.fileName as string]: progress.percent,
      }));
    }
  };

  const handleUpload = async () => {
    if (!files.length || loading) {
      return;
    }
    setProgresses({});
    setLatestProgress(null);
    await onOk?.(files, { onProgress: handleProgress });
  };

  const phaseText = latestProgress
    ? {
        hashing: t('fileManager.hashingFile'),
        init: t('fileManager.preparingUpload'),
        resume: t('fileManager.resumingUpload'),
        uploading: t('fileManager.uploadingFile'),
        merging: t('fileManager.mergingFile'),
        done: t('fileManager.uploadDone'),
      }[latestProgress.phase]
    : '';

  return (
    <Dialog open onOpenChange={hideModal}>
      <DialogContent className="sm:max-w-[425px]">
        <DialogHeader>
          <DialogTitle>{t('fileManager.uploadFile')}</DialogTitle>
        </DialogHeader>
        <UploaderTabs
          files={files}
          setFiles={setFiles}
          disabled={loading}
          progresses={progresses}
        ></UploaderTabs>
        {loading && latestProgress ? (
          <div className="flex flex-col gap-2 rounded-lg border p-3">
            <div className="flex items-center justify-between text-sm">
              <span className="truncate">
                {phaseText}
                {latestProgress.fileName ? `：${latestProgress.fileName}` : ''}
              </span>
              <span className="shrink-0 text-muted-foreground">
                {latestProgress.phase === 'uploading' &&
                latestProgress.totalParts
                  ? `${latestProgress.uploadedParts}/${latestProgress.totalParts} · `
                  : ''}
                {latestProgress.percent}%
              </span>
            </div>
            <Progress value={latestProgress.percent} />
          </div>
        ) : null}
        <DialogFooter>
          <Button
            type="button"
            variant={'tertiary'}
            size={'sm'}
            onClick={handleUpload}
            disabled={!files.length || loading}
          >
            {loading ? t('common.uploading') : t('common.save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
